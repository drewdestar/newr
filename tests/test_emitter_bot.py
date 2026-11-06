"""Tests for the bot emitter (compiler/emitter.py, M2, part 4b).

The bot emitter turns a checked bot program into a discord.py bot:
the v0 golden template (examples/hello.py, examples/showcase.py) is
the reference shape — a `Bot` subclass, module-level `cmd_*`
functions, `@bot.tree.error`, `_channel_text`, `main()`.

Text-assertion tests run in the normal suite (they need no discord).
Mock round-trip tests (a fake interaction calls a `cmd_*` function
and the recorded reply is asserted) need discord.py 2.7.1, which
lives only in the `newr-verify` venv — they skip without it and run
under `~/workspace/venvs/newr-verify/bin/python -m unittest`.
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.checker import check_program
from compiler.discord_check import check_discord, discord_providers
from compiler.emitter import emit_program
from compiler.resolver import resolve_file
from tests.test_emitter import emit_main_text, emit_src, full_program

try:
    import discord  # noqa: F401  (only the newr-verify venv has it)
    from discord import app_commands  # noqa: F401
    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False


HELLO_BOT = """bot "HelloBot":
  description: "A friendly bot that answers in plain words"

command ping:
  description: "Answer with pong"
  reply: "Pong! Hello {user}"

command greet:
  description: "Greet someone in this server"
  reply: "Hello {user}, welcome to {guild}!"
  ephemeral: true

event ready:
  log: "{bot} is online"

event member_join:
  reply: "Welcome to {guild}, {user}!"
"""

SHOP = """import "shop_items.newr"

bot "ShopBot":
  description: "A tiny shop"
  intents: [guilds, members]

strings en:
  "buy.ok": "Bought {item} for {price} gold. Balance: {balance}."
  "buy.poor": "Not enough gold — {item} costs {price}, you have {balance}."
strings id:
  "buy.ok": "Membeli {item} seharga {price} gold. Saldo: {balance}."
  "buy.poor": "Gold tidak cukup — {item} harganya {price}, kamu punya {balance}."

store economy:
  table balances (user: str, balance: int)
  ledger true

command ping:
  description: "Answer with pong"
  reply: "Pong! Hello {user}"

command buy:
  description: "Buy an item from the shop"
  option item: str (required, choices: ["brick", "potion"])
  option amount: int (default: 1, min: 1, max: 99)
  do:
    let price: int = shop_items.price_of(item) * amount
    let bal: int = economy.balance(user)
    if bal < price:
      reply t("buy.poor", price: price, balance: bal) ephemeral
    else:
      economy.credit(user, -price, key: "buy-{user.id}-{item}-{amount}")
      reply t("buy.ok", item: item, price: price, balance: bal - price)
      button "Buy again" (style: primary, id: "buy-again", timeout: 60s):
        only: author
        do:
          reply "Use /buy again, {user}!"
        on_timeout:
          reply "Offer expired."

command balance:
  description: "Show your balance"
  do:
    reply "Balance: {economy.balance(user)} gold."

event ready:
  log: "{bot} is online"

event member_join:
  reply: "Welcome to {guild}, {user}!"

task every 1h:
  do:
    log "Hourly shop check ran"
"""


def _emit_shop_result():
    """Emit SHOP from a real temp dir so its `import "shop_items.newr"`
    resolves (the shop_items source is copied from examples/).
    Returns the full EmitResult (all emitted files)."""
    root = Path(__file__).resolve().parent.parent
    items = (root / "examples" / "shop_items.newr").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "shop_items.newr").write_text(items, encoding="utf-8")
        main = Path(d) / "shop.newr"
        main.write_text(SHOP, encoding="utf-8")
        resolved = resolve_file(main)
        diags = list(resolved.diagnostics)
        diags += check_program(
            resolved.program,
            providers=discord_providers(resolved.program)).diagnostics
        diags += check_discord(resolved.program)
        errs = [x for x in diags if x.is_error()]
        assert not errs, [f"{x.code}: {x.message}" for x in errs]
        sources = {
            str(main): SHOP,
            str(Path(d) / "shop_items.newr"): items,
        }
        return emit_program(resolved.program, main_path=str(main),
                            sources=sources)


def _run_shop_bot(scenario):
    """Emit SHOP into a temp dir, import it there, and run
    ``scenario(bot)`` (an async function taking the Bot instance).

    The temp dir is the CWD while the scenario runs, so the
    `economy.db` file lands there instead of in the repo. The db
    is closed before the dir is removed.
    """
    import importlib.util
    import os
    files = _emit_shop_result().files
    root = Path(__file__).resolve().parent.parent
    rt = (root / "compiler" / "newr_rt.py").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as d:
        for name, text in files.items():
            (Path(d) / name).write_text(text, encoding="utf-8")
        (Path(d) / "newr_rt.py").write_text(rt, encoding="utf-8")
        sys.path.insert(0, d)
        old_cwd = os.getcwd()
        os.chdir(d)
        try:
            spec = importlib.util.spec_from_file_location(
                "shop_economy_test", Path(d) / "shop.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["shop_economy_test"] = mod
            spec.loader.exec_module(mod)

            async def _wrapped():
                bot = mod.bot
                try:
                    return await scenario(bot)
                finally:
                    if bot.economy_db is not None:
                        await bot.economy_db.close()
            return asyncio.run(_wrapped())
        finally:
            os.chdir(old_cwd)
            sys.path.remove(d)
            sys.modules.pop("shop_economy_test", None)
            sys.modules.pop("shop_items", None)
            sys.modules.pop("newr_rt", None)


def emit_shop_text():
    """Emit SHOP from a real temp dir so its `import \"shop_items.newr\"`
    resolves (the shop_items source is copied from examples/)."""
    result = _emit_shop_result()
    return result.files[result.main_file]


class TestBotStructure(unittest.TestCase):
    def test_hello_bot_shape(self):
        text = emit_main_text(HELLO_BOT)
        self.assertIn("import discord", text)
        self.assertIn("from discord import app_commands", text)
        self.assertIn("from discord.ext import commands", text)
        self.assertIn('log = logging.getLogger("hellobot")', text)
        self.assertIn("class Bot(commands.Bot):", text)
        self.assertIn('super().__init__(command_prefix="!", intents=intents)',
                      text)
        self.assertIn("async def setup_hook(self) -> None:", text)
        self.assertIn("await self.tree.sync()", text)
        self.assertIn("async def on_ready(self) -> None:", text)
        self.assertIn('log.info(f"{self.user} is online")', text)
        self.assertIn("async def on_member_join(self, member: discord.Member)",
                      text)
        self.assertIn("if channel is not None:", text)
        self.assertIn("bot = Bot()", text)
        self.assertIn("@bot.tree.error", text)
        self.assertIn("def _channel_text(channel) -> str:", text)
        self.assertIn("def main() -> None:", text)
        self.assertIn('os.environ.get("DISCORD_TOKEN")', text)
        self.assertIn('if __name__ == "__main__":', text)

    def test_intents_from_declaration_and_events(self):
        # members=True because member_join is present; message_content
        # stays off (no message event); presences off (not declared).
        text = emit_main_text(HELLO_BOT)
        self.assertIn("intents.members = True", text)
        self.assertNotIn("message_content", text)
        self.assertNotIn("presences", text)
        # An explicit intents declaration is honoured.
        text2 = emit_main_text("bot \"B\":\n  intents: [guilds, message_content]\n"
                               "command ping:\n  reply: \"pong\"\n")
        self.assertIn("intents.message_content = True", text2)
        self.assertNotIn("intents.members", text2)

    def test_command_functions_and_ephemeral(self):
        text = emit_main_text(HELLO_BOT)
        self.assertIn('@bot.tree.command(name="ping", description="Answer with pong")',
                      text)
        self.assertIn("async def cmd_ping(interaction: discord.Interaction)",
                      text)
        self.assertIn("await interaction.response.send_message("
                      'f"Pong! Hello {interaction.user.mention}")', text)
        self.assertIn("ephemeral=True", text)
        # The error handler is only emitted when commands exist.
        self.assertIn("@bot.tree.error", text)

    def test_no_commands_no_error_handler(self):
        text = emit_main_text("bot \"B\":\n"
        "  description: \"B\"\n"
                              "event ready:\n"
                              "  log: \"{bot} is online\"\n")
        self.assertNotIn("@bot.tree.error", text)

    def test_on_message_ignores_own_messages(self):
        # A message event answers with `send` in a `do:` body, not
        # with a reply (E2008).
        text = emit_main_text("bot \"B\":\n  intents: [message_content]\n"
                              "event message:\n"
                              "  do:\n"
                              "    send \"echo {message.content}\" to message.channel\n")
        self.assertIn("async def on_message(self, message: discord.Message)",
                      text)
        self.assertIn("if message.author == self.user:", text)
        self.assertIn("return", text)


class TestBotOptions(unittest.TestCase):
    def test_option_types_and_defaults(self):
        src = ("bot \"B\":\n"
        "  description: \"B\"\n"
               "command buy:\n"
               "  option item: str (required)\n"
               "  option amount: int (default: 1)\n"
               "  option ratio: num (default: 1.5)\n"
               "  option fast: bool (default: true)\n"
               "  option target: user\n"
               "  option chan: channel\n"
               "  option role: role\n"
               "  do:\n"
               "    reply \"ok\"\n")
        text = emit_main_text(src)
        self.assertIn("async def cmd_buy(interaction: discord.Interaction, "
                      "item: str, amount: int = 1, ratio: float = 1.5, "
                      "fast: bool = True, target: discord.Member | None = None, "
                      "chan: discord.abc.GuildChannel | None = None, "
                      "role: discord.Role | None = None)", text)

    def test_option_choices_and_range(self):
        # D15: choices is a decorator; Range is a generic subscript
        # (Range(1, 10) is a TypeError in discord.py 2.7.1).
        text = emit_shop_text()
        self.assertIn('@app_commands.choices(item=[app_commands.Choice(name="brick", value="brick"), '
                      'app_commands.Choice(name="potion", value="potion")])', text)
        self.assertIn("amount: app_commands.Range[int, 1, 99] = 1", text)
        self.assertIn('@app_commands.describe(item="item", amount="amount")',
                      text)

    def test_option_description(self):
        src = ("bot \"B\":\n"
        "  description: \"B\"\n"
               "command buy:\n"
               "  option item: str (required, description: \"the item\")\n"
               "  do:\n"
               "    reply \"ok\"\n")
        text = emit_main_text(src)
        self.assertIn('@app_commands.describe(item="the item")', text)


class TestBotStatements(unittest.TestCase):
    def test_reply_defer_edit_followup_send(self):
        # `send` is only allowed in a message event or a task body
        # (E2008), so the send form is tested in a message event.
        src = ("bot \"B\":\n"
        "  description: \"B\"\n"
        "  intents: [message_content]\n"
               "command x:\n"
               "  do:\n"
               "    defer\n"
               "    reply \"first\"\n"
               "    edit \"second\"\n"
               "    followup \"third\"\n"
               "event message:\n"
               "  do:\n"
               "    send \"hello\" to message.channel\n")
        text = emit_main_text(src)
        self.assertIn("await interaction.response.defer()", text)
        self.assertIn('await interaction.response.send_message("first")',
                      text)
        self.assertIn('await interaction.edit_original_response(content="second")',
                      text)
        self.assertIn('await interaction.followup.send("third")', text)
        self.assertIn('await message.channel.send("hello")', text)

    def test_embed_reply(self):
        src = ("bot \"B\":\n"
        "  description: \"B\"\n"
               "command x:\n"
               "  do:\n"
               "    reply embed:\n"
               "      title: \"Hi {user}\"\n"
               "      description: \"welcome\"\n"
               "      color: 0x5865F2\n"
               "      field \"A\": \"1\" (inline: true)\n")
        text = emit_main_text(src)
        self.assertIn("embed = discord.Embed(", text)
        self.assertIn("title=f\"Hi {interaction.user.mention}\"", text)
        self.assertIn('description="welcome"', text)
        # A hex literal is emitted as its decimal value (the parser
        # keeps the int, not the source spelling): 0x5865F2 = 5793266.
        self.assertIn("color=5793266", text)
        self.assertIn("embed.add_field(name='A', value=\"1\", inline=True)",
                      text)
        self.assertIn("await interaction.response.send_message(embed=embed)",
                      text)

    def test_log_levels_in_bot(self):
        src = ("bot \"B\":\n"
        "  description: \"B\"\n"
               "command x:\n"
               "  do:\n"
               "    log \"info\"\n"
               "    log.warn(\"warn\")\n"
               "    log.error(\"err\")\n"
               "    reply \"ok\"\n")
        text = emit_main_text(src)
        self.assertIn('log.info("info")', text)
        self.assertIn('log.warning("warn")', text)
        self.assertIn('log.error("err")', text)

    def test_await_inside_fstring_field(self):
        # shop's balance reply: the store call is awaited inside the
        # f-string field (D15 verification — must be real Python).
        text = emit_shop_text()
        self.assertIn("f\"Balance: {await bot.economy_balance("
                      "str(interaction.user.id))} gold.\"", text)


class TestBotStrings(unittest.TestCase):
    def test_t_call_and_strings_dict(self):
        text = emit_shop_text()
        self.assertIn('STRINGS = {', text)
        self.assertIn('"en": {', text)
        self.assertIn('"buy.ok": "Bought {item} for {price} gold. '
                      'Balance: {balance}."', text)
        self.assertIn('"id": {', text)
        self.assertIn("def _t(key, locale, **kwargs) -> str:", text)
        self.assertIn("await interaction.response.send_message(_t(", text)

    def test_t_call_handler_binding_placeholder(self):
        # A template placeholder that is a handler binding (user) is
        # auto-passed — the command does not list it as an argument.
        src = ("bot \"B\":\n"
               "  description: \"B\"\n"
               "strings en:\n"
               "  \"hi\": \"Hello {user}!\"\n"
               "command x:\n"
               "  do:\n"
               "    reply t(\"hi\")\n")
        text = emit_main_text(src)
        self.assertIn("_t(\"hi\", interaction.locale, "
                      "user=interaction.user.mention)", text)


class TestBotStore(unittest.TestCase):
    def test_store_methods_on_bot(self):
        text = emit_shop_text()
        self.assertIn("import aiosqlite", text)
        self.assertIn("self.economy_db = None", text)
        self.assertIn("await self._economy_connect()", text)
        self.assertIn("async def economy_balance(self, user_id: str) -> int:",
                      text)
        self.assertIn("async def economy_credit(self, user_id: str, amount: int, key: str | None = None) -> None:",
                      text)
        self.assertIn("INSERT INTO ledger", text)
        self.assertIn("await bot.economy_balance(str(interaction.user.id))",
                      text)

    def test_store_key_from_interpolation(self):
        # The credit key is an interpolated string: {user.id} etc.
        text = emit_shop_text()
        self.assertIn('await bot.economy_credit(str(interaction.user.id), neg(price), key=f"buy-{interaction.user.id}-{item}-{amount}")',
                      text)

    def test_economy_uses_lock_and_explicit_transactions(self):
        # M2 evaluation must-fix #2: the economy ops must be atomic
        # — one explicit transaction per op, serialised by a lock
        # (aiosqlite runs all ops in one thread, so a second
        # coroutine's BEGIN IMMEDIATE would block it).
        text = emit_shop_text()
        self.assertIn("import asyncio", text)
        self.assertIn("self.economy_lock = None", text)
        self.assertIn("self.economy_lock = asyncio.Lock()", text)
        self.assertIn("BEGIN IMMEDIATE", text)
        self.assertIn("ROLLBACK", text)
        self.assertIn("async with self.economy_lock:", text)
        # No more two separate _apply calls for one transfer.
        self.assertNotIn("await self._economy_apply(from_id, -amount",
                         text)


class TestBotComponents(unittest.TestCase):
    def test_button_view(self):
        text = emit_shop_text()
        self.assertIn("class BuyAgainView(discord.ui.View):", text)
        self.assertIn("@discord.ui.button(label=\"Buy again\", style=discord.ButtonStyle.primary, custom_id=\"buy-again\")",
                      text)
        self.assertIn("async def _on_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:",
                      text)
        self.assertIn("if self._author_id is not None and interaction.user.id != self._author_id:", text)
        self.assertIn("await interaction.response.send_message(", text)
        self.assertIn("view=buy_again_view)", text)
        self.assertIn("timeout=60", text)

    def test_select(self):
        # A select attaches to a preceding reply (it does not answer
        # by itself), and its `values` (list[str]) is joined for
        # display — a list cannot be interpolated (E2001).
        src = ("bot \"B\":\n"
        "  description: \"B\"\n"
               "command x:\n"
               "  do:\n"
               "    reply \"pick one\"\n"
               "    select \"Pick one\" (options: [\"a\", \"b\"], timeout: 30s):\n"
               "      only: author\n"
               "      do:\n"
               "        reply \"you picked {\", \".join(values)}\"\n")
        text = emit_main_text(src)
        self.assertIn("class PickOneSelect(discord.ui.Select):", text)
        self.assertIn('discord.SelectOption(label="a", value="a")', text)
        self.assertIn("self.values", text)

    def test_modal(self):
        src = ("bot \"B\":\n"
        "  description: \"B\"\n"
               "command x:\n"
               "  do:\n"
               "    modal \"Feedback\" (timeout: 120s):\n"
               "      text_input \"name\" (style: short, required):\n"
               "      text_input \"notes\" (style: paragraph):\n"
               "      do:\n"
               "        reply \"thanks {name}\"\n")
        text = emit_main_text(src)
        self.assertIn("class FeedbackModal(discord.ui.Modal):", text)
        self.assertIn("name = discord.ui.TextInput(label=\"name\", style=discord.TextStyle.short, required=True)",
                      text)
        self.assertIn("notes = discord.ui.TextInput(label=\"notes\", style=discord.TextStyle.paragraph, required=False)",
                      text)
        self.assertIn("async def on_submit(self, interaction: discord.Interaction)",
                      text)

    def test_task_loop(self):
        # A task has no name in newr, so the loop function is named
        # for its interval.
        text = emit_shop_text()
        self.assertIn("@tasks.loop(hours=1.0)", text)
        self.assertIn("async def _every_1h() -> None:", text)
        self.assertIn("_every_1h.start()", text)
        self.assertIn("@_every_1h.before_loop", text)
        self.assertIn("await bot.wait_until_ready()", text)


class TestBotBindings(unittest.TestCase):
    def test_handler_binding_forms(self):
        # Value form vs interpolation form, per binding (the v0
        # golden: user -> interaction.user / .mention; guild -> name
        # with a DM fallback; channel -> _channel_text).
        text = emit_main_text(HELLO_BOT)
        self.assertIn('f"Pong! Hello {interaction.user.mention}"', text)
        self.assertIn("interaction.guild.name if interaction.guild else 'DM'",
                      text)
        text2 = emit_main_text("bot \"B\":\n"
        "  description: \"B\"\n"
                               "command x:\n"
                               "  do:\n"
                               "    reply \"in {channel} on {guild}\"\n")
        self.assertIn("_channel_text(interaction.channel)", text2)

    def test_member_join_bindings(self):
        text = emit_main_text(HELLO_BOT)
        self.assertIn('f"Welcome to {member.guild.name}, {member.mention}!"',
                      text)

    def test_ready_bot_binding(self):
        text = emit_main_text(HELLO_BOT)
        self.assertIn('log.info(f"{self.user} is online")', text)


# ---------------------------------------------------------------- mock round-trip

class _FakeResponse:
    def __init__(self):
        self.sent = []
        self.deferred = False
        self.edited = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    async def defer(self, **kwargs):
        self.deferred = True

    async def edit_original_response(self, **kwargs):
        self.edited.append(kwargs)


class _FakeUser:
    def __init__(self, uid=42, name="tester"):
        self.id = uid
        self.name = name
        self.mention = f"<@{uid}>"


class _FakeGuild:
    def __init__(self, name="Testville"):
        self.name = name
        self.system_channel = None


class _FakeChannel:
    def __init__(self):
        self.mention = "#general"


class _FakeInteraction:
    def __init__(self, guild=True):
        self.user = _FakeUser()
        self.guild = _FakeGuild() if guild else None
        self.channel = _FakeChannel()
        self.response = _FakeResponse()
        self.locale = "en"

    @property
    def followup(self):
        return self.response


def _load_bot_module(text, name):
    """Compile + exec emitted bot source in a fresh module namespace."""
    import types
    mod = types.ModuleType(name)
    mod.__dict__["__name__"] = name
    exec(compile(text, name, "exec"), mod.__dict__)
    return mod


@unittest.skipUnless(HAS_DISCORD, "discord.py 2.7.1 only in newr-verify venv")
class TestBotMockRoundTrip(unittest.TestCase):
    def test_ping_and_greet_replies(self):
        result = emit_src(HELLO_BOT)
        text = result.files[result.main_file]
        mod = _load_bot_module(text, "hello_bot_mock")
        inter = _FakeInteraction()
        asyncio.run(mod.cmd_ping.callback(inter))
        self.assertEqual(inter.response.sent[0][0], "Pong! Hello <@42>")
        inter2 = _FakeInteraction()
        asyncio.run(mod.cmd_greet.callback(inter2))
        self.assertEqual(inter2.response.sent[0][0],
                         "Hello <@42>, welcome to Testville!")
        self.assertTrue(inter2.response.sent[0][1].get("ephemeral"))
        # The error handler is registered on the tree.
        self.assertIsNotNone(mod.bot.tree.get_command("ping"))
        self.assertIsNotNone(mod.bot.tree.get_command("greet"))

    def test_greet_in_dm(self):
        result = emit_src(HELLO_BOT)
        mod = _load_bot_module(result.files[result.main_file],
                               "hello_bot_dm_mock")
        inter = _FakeInteraction(guild=False)
        asyncio.run(mod.cmd_greet.callback(inter))
        self.assertEqual(inter.response.sent[0][0],
                         "Hello <@42>, welcome to DM!")

    def test_member_join(self):
        result = emit_src(HELLO_BOT)
        mod = _load_bot_module(result.files[result.main_file],
                               "hello_bot_join_mock")

        class _FakeMember:
            def __init__(self):
                self.mention = "<@7>"
                self.guild = _FakeGuild()

        sent = []

        class _Chan:
            async def send(self, content):
                sent.append(content)

        member = _FakeMember()
        member.guild.system_channel = _Chan()
        asyncio.run(mod.bot.on_member_join(member))
        self.assertEqual(sent, ["Welcome to Testville, <@7>!"])

    def test_channel_text(self):
        result = emit_src(HELLO_BOT)
        mod = _load_bot_module(result.files[result.main_file],
                               "hello_bot_chan_mock")
        self.assertEqual(mod._channel_text(_FakeChannel()), "#general")
        self.assertEqual(mod._channel_text(None), "")

    def test_transfer_fresh_replay_and_insufficient(self):
        async def scenario(bot):
            await bot._economy_connect()
            await bot.economy_credit("alice", 100)
            self.assertTrue(await bot.economy_transfer(
                "alice", "bob", 50, key="K1"))
            self.assertEqual(await bot.economy_balance("alice"), 50)
            self.assertEqual(await bot.economy_balance("bob"), 50)
            # Replay: same key is a no-op, still success.
            self.assertTrue(await bot.economy_transfer(
                "alice", "bob", 50, key="K1"))
            self.assertEqual(await bot.economy_balance("alice"), 50)
            self.assertEqual(await bot.economy_balance("bob"), 50)
            # Insufficient: no money moves, returns False.
            self.assertFalse(await bot.economy_transfer(
                "alice", "bob", 60, key="K2"))
            self.assertEqual(await bot.economy_balance("alice"), 50)
            self.assertEqual(await bot.economy_balance("bob"), 50)
        _run_shop_bot(scenario)

    def test_transfer_without_key_is_atomic(self):
        async def scenario(bot):
            await bot._economy_connect()
            await bot.economy_credit("alice", 100)
            self.assertTrue(await bot.economy_transfer("alice", "bob", 40))
            self.assertEqual(await bot.economy_balance("alice"), 60)
            self.assertEqual(await bot.economy_balance("bob"), 40)
        _run_shop_bot(scenario)

    def test_transfer_crash_between_legs_then_retry_heals(self):
        # M2 evaluation must-fix #2: seed alice 100, simulate a
        # crash between the two legs (out-leg committed, in-leg
        # never ran), then retry — the retry must HEAL the transfer
        # (credit bob the missing 50), not report success on a
        # half-done transfer. Total money is conserved (100).
        async def scenario(bot):
            await bot._economy_connect()
            await bot.economy_credit("alice", 100)
            async with bot.economy_db.execute(
                    "INSERT INTO balances (user, balance) VALUES (?, ?) "
                    "ON CONFLICT(user) DO UPDATE SET balance = "
                    "balance + excluded.balance", ("alice", -50)):
                pass
            async with bot.economy_db.execute(
                    "INSERT INTO ledger (user, amount, key, created_at) "
                    "VALUES (?, ?, ?, ?)", ("alice", -50, "K:out", 0)):
                pass
            await bot.economy_db.commit()
            self.assertTrue(await bot.economy_transfer(
                "alice", "bob", 50, key="K"))
            self.assertEqual(await bot.economy_balance("alice"), 50)
            self.assertEqual(await bot.economy_balance("bob"), 50)
            # A second retry is an idempotent no-op.
            self.assertTrue(await bot.economy_transfer(
                "alice", "bob", 50, key="K"))
            self.assertEqual(await bot.economy_balance("alice"), 50)
            self.assertEqual(await bot.economy_balance("bob"), 50)
        _run_shop_bot(scenario)

    def test_transfer_with_in_leg_poisoned_then_retry_heals(self):
        # The other poisoning: a synthetic `K:in` row (amount 0)
        # with no money moved. The retry must still move the full
        # 50 in both legs.
        async def scenario(bot):
            await bot._economy_connect()
            await bot.economy_credit("alice", 100)
            async with bot.economy_db.execute(
                    "INSERT INTO ledger (user, amount, key, created_at) "
                    "VALUES (?, ?, ?, ?)", ("bob", 0, "K:in", 0)):
                pass
            await bot.economy_db.commit()
            self.assertTrue(await bot.economy_transfer(
                "alice", "bob", 50, key="K"))
            self.assertEqual(await bot.economy_balance("alice"), 50)
            self.assertEqual(await bot.economy_balance("bob"), 50)
        _run_shop_bot(scenario)

    def test_concurrent_debits_cannot_overdraw(self):
        # Two concurrent debits of 60 against a balance of 100:
        # exactly one succeeds; the balance ends at 40, never -20
        # (the balance check is inside the transaction, and the
        # lock serialises the ops).
        async def scenario(bot):
            await bot._economy_connect()
            await bot.economy_credit("alice", 100)
            results = await asyncio.gather(
                bot.economy_debit("alice", 60, key="D1"),
                bot.economy_debit("alice", 60, key="D2"))
            self.assertEqual(sorted(results), [False, True])
            self.assertEqual(await bot.economy_balance("alice"), 40)
        _run_shop_bot(scenario)

    def test_credit_idempotent_on_key(self):
        async def scenario(bot):
            await bot._economy_connect()
            await bot.economy_credit("alice", 100, key="C1")
            await bot.economy_credit("alice", 100, key="C1")
            self.assertEqual(await bot.economy_balance("alice"), 100)
        _run_shop_bot(scenario)

    def test_debit_replay_returns_true_without_double_debit(self):
        async def scenario(bot):
            await bot._economy_connect()
            await bot.economy_credit("alice", 100)
            self.assertTrue(await bot.economy_debit("alice", 30, key="D1"))
            self.assertTrue(await bot.economy_debit("alice", 30, key="D1"))
            self.assertEqual(await bot.economy_balance("alice"), 70)
        _run_shop_bot(scenario)


if __name__ == "__main__":
    unittest.main()
