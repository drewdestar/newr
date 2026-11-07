"""Tests for the JavaScript bot emitter (compiler/emitter_js.py, M3).

The JS twin of tests/test_emitter_bot.py: the same bot programs
(HELLO_BOT, SHOP) must emit discord.js 14 code with the same shape
and behaviour as the Python target — a `Bot extends Client`, a
`commands` array + commandMap, a `componentHandlers` map, a
standalone `<stem>_deploy.js`, store methods on Bot with the same
ledger + idempotency + heal semantics.

Text-assertion tests run in the normal suite (they need no
discord.js). Mock round-trip tests run the emitted bot under Node
against tests/js/mocks/discord.js (a stand-in implementing the API
surface the emitted code touches) plus tests/js/bot-driver-lib.js
(fake interactions); they need only Node, so they run in the normal
suite too — no live Discord, no real discord.js.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.checker import check_program
from compiler.discord_check import check_discord, discord_providers
from compiler.emitter_js import emit_program
from compiler.resolver import resolve_file
from tests.test_emitter_js import emit_js_main_text, emit_js_src, full_program

ROOT = Path(__file__).resolve().parent.parent
RT_JS_SRC = ROOT / "compiler" / "newr_rt.js"
MOCKS_DIR = ROOT / "tests" / "js" / "mocks"
DRIVER_LIB = ROOT / "tests" / "js" / "bot-driver-lib.js"
NODE = shutil.which("node")

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
    items = (ROOT / "examples" / "shop_items.newr").read_text(encoding="utf-8")
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


def emit_shop_text():
    """Emit SHOP from a real temp dir; the main file's text."""
    result = _emit_shop_result()
    return result.files[result.main_file]


def run_bot_driver(driver_js, files, extra_files=None):
    """Run a driver JS file under Node against the emitted files.

    `files` maps an output basename to its text (the EmitResult's
    files); newr_rt.js and bot-driver-lib.js are copied in; the
    mock discord.js is put on NODE_PATH so `require("discord.js")`
    resolves to it. The driver prints one JSON object on its last
    line; returns the parsed object. The temp dir is the CWD, so a
    store's `<store>.db` lands there."""
    env = dict(os.environ)
    env["NODE_PATH"] = str(MOCKS_DIR)
    with tempfile.TemporaryDirectory() as d:
        for name, text in files.items():
            (Path(d) / name).write_text(text, encoding="utf-8")
        (Path(d) / "newr_rt.js").write_text(
            RT_JS_SRC.read_text(encoding="utf-8"), encoding="utf-8")
        (Path(d) / "bot-driver-lib.js").write_text(
            DRIVER_LIB.read_text(encoding="utf-8"), encoding="utf-8")
        for name, text in (extra_files or {}).items():
            (Path(d) / name).write_text(text, encoding="utf-8")
        (Path(d) / "driver.js").write_text(driver_js, encoding="utf-8")
        proc = subprocess.run(
            [NODE, str(Path(d) / "driver.js")],
            capture_output=True, text=True, cwd=d, env=env)
        assert proc.returncode == 0, (
            f"driver exit {proc.returncode}\nstdout: {proc.stdout}\n"
            f"stderr: {proc.stderr}")
        last = proc.stdout.strip().splitlines()[-1]
        return json.loads(last)


def run_shop_driver(driver_js):
    """Run a driver against the emitted SHOP bot."""
    return run_bot_driver(driver_js, _emit_shop_result().files)


@unittest.skipUnless(NODE, "node is not installed")
class TestBotStructure(unittest.TestCase):
    def test_hello_bot_shape(self):
        text = emit_js_main_text(HELLO_BOT)
        self.assertIn("class Bot extends Client", text)
        self.assertIn("const bot = new Bot();", text)
        self.assertIn("const commands = [", text)
        self.assertIn("const commandMap = new Map(", text)
        self.assertIn("bot.on(Events.InteractionCreate,", text)
        # Registration is a separate file, never on ready (J10).
        self.assertNotIn("application.commands.set", text)
        self.assertNotIn("Routes.applicationCommands", text)

    def test_deploy_file_is_standalone_rest_put(self):
        result = emit_js_src(HELLO_BOT, "hello_bot.newr")
        self.assertIn("hello_bot_deploy.js", result.files)
        deploy = result.files["hello_bot_deploy.js"]
        self.assertIn('require("./hello_bot.js")', deploy)
        self.assertIn("new REST({ version: \"10\" })", deploy)
        self.assertIn("Routes.applicationGuildCommands", deploy)
        self.assertIn("Routes.applicationCommands", deploy)
        self.assertIn("DISCORD_GUILD_ID", deploy)
        # Requiring the bot file from the deploy script must not run
        # main() (the require.main guard).
        self.assertIn("require.main === module", result.files["hello_bot.js"])

    def test_deploy_file_header_names_the_source(self):
        # The deploy file's header names the .newr source (like
        # every other emitted file), not the deploy file itself.
        result = emit_js_src(HELLO_BOT, "hello_bot.newr")
        deploy = result.files["hello_bot_deploy.js"]
        self.assertIn("// Generated by newr from hello_bot.newr —",
                      deploy)
        self.assertIn("// Edit the .newr file and rebuild with: "
                      "newr build hello_bot.newr", deploy)
        self.assertNotIn("hello_bot_deploy.js", deploy.splitlines()[0])

    def test_intents_from_declaration_and_events(self):
        text = emit_shop_text()
        self.assertIn("GatewayIntentBits.Guilds", text)
        # members: declared AND needed by member_join.
        self.assertIn("GatewayIntentBits.GuildMembers", text)
        hello = emit_js_main_text(HELLO_BOT)
        # member_join adds members even when not declared.
        self.assertIn("GatewayIntentBits.GuildMembers", hello)
        # No message event, so no message_content.
        self.assertNotIn("MessageContent", hello)

    def test_command_functions_and_ephemeral(self):
        text = emit_js_main_text(HELLO_BOT)
        self.assertIn('setName("ping")', text)
        self.assertIn('setName("greet")', text)
        self.assertIn("MessageFlags.Ephemeral", text)
        self.assertIn("async function dispatchCommand(", text)
        self.assertIn("interaction.replied || interaction.deferred", text)

    def test_no_commands_no_error_handler(self):
        text = emit_js_main_text(
            "bot \"B\":\n  description: \"B\"\n\nevent ready:\n  log: \"hi\"\n")
        self.assertNotIn("dispatchCommand", text)
        self.assertNotIn("const commands", text)

    def test_on_message_ignores_own_messages(self):
        # A message event answers with `send`, not `reply` (E2008).
        text = emit_js_main_text(
            "bot \"B\":\n  description: \"B\"\n\nevent message:\n"
            "  do:\n"
            "    send \"echo {message}\" to message.channel\n")
        self.assertIn("if (message.author.id === bot.user.id) {", text)
        self.assertIn("bot.on(Events.MessageCreate,", text)
        self.assertIn("GatewayIntentBits.MessageContent", text)


class TestBotOptions(unittest.TestCase):
    def test_option_types_and_defaults(self):
        text = emit_shop_text()
        self.assertIn("addStringOption", text)
        self.assertIn("addIntegerOption", text)
        self.assertIn('getString("item", true)', text)
        # An int option converts the JS number to a BigInt.
        self.assertIn('BigInt(interaction.options.getInteger("amount") ?? 1n)',
                      text)

    def test_option_choices_and_range(self):
        text = emit_shop_text()
        self.assertIn('.addChoices({ name: "brick", value: "brick" }',
                      text)
        # setMinValue/setMaxValue take JS numbers, not BigInts.
        self.assertIn(".setMinValue(1)", text)
        self.assertIn(".setMaxValue(99)", text)
        self.assertNotIn("setMinValue(1n)", text)

    def test_option_description(self):
        text = emit_js_main_text(
            "bot \"B\":\n"
            "  description: \"B\"\n"
            "command buy:\n"
            "  option item: str (required, description: \"the item\")\n"
            "  do:\n"
            "    reply \"ok\"\n")
        self.assertIn('.setDescription("the item")', text)


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
        text = emit_js_main_text(src)
        self.assertIn("await interaction.deferReply();", text)
        self.assertIn('await interaction.reply({ content: "first" });', text)
        self.assertIn('await interaction.editReply({ content: "second" });',
                      text)
        self.assertIn('await interaction.followUp({ content: "third" });',
                      text)
        self.assertIn('await message.channel.send({ content: "hello" });',
                      text)

    def test_embed_reply(self):
        src = ("bot \"B\":\n"
               "  description: \"B\"\n"
               "command a:\n"
               "  do:\n"
               "    reply embed:\n"
               "      title: \"T\"\n"
               "      field \"F\": \"V\"\n")
        text = emit_js_main_text(src)
        self.assertIn("new EmbedBuilder()", text)
        self.assertIn('.setTitle("T")', text)
        self.assertIn('addFields({ name: "F", value: "V", inline: false })',
                      text)
        self.assertIn("embeds: [", text)

    def test_log_levels_in_bot(self):
        # A command must end in a reply (E2010), so the logs are
        # followed by one.
        src = ("bot \"B\":\n"
               "  description: \"B\"\n"
               "command a:\n"
               "  do:\n"
               "    log \"info\"\n"
               "    log.warn(\"warn\")\n"
               "    log.error(\"err\")\n"
               "    reply \"ok\"\n")
        text = emit_js_main_text(src)
        self.assertIn('console.info("info")', text)
        self.assertIn('console.warn("warn")', text)
        self.assertIn('console.error("err")', text)

    def test_await_inside_fstring_field(self):
        # The shop's balance reply: a store call inside an
        # interpolation is an await inside a template literal.
        text = emit_shop_text()
        self.assertIn(
            "`Balance: ${await bot.economy_balance(String(interaction.user.id))} gold.`",
            text)


class TestBotStrings(unittest.TestCase):
    def test_t_call_and_strings_dict(self):
        text = emit_shop_text()
        self.assertIn("const _DEFAULT_LOCALE = \"en\";", text)
        self.assertIn('STRINGS = {', text)
        self.assertIn('"buy.ok": "Bought {item} for {price} gold. Balance: {balance}.",', text)
        self.assertIn('"id": {', text)
        self.assertIn("function _t(key, locale, vars = {}) {", text)
        self.assertIn('_t("buy.ok", interaction.locale, ', text)

    def test_t_call_handler_binding_placeholder(self):
        # A placeholder filled by a handler binding (user) needs no
        # explicit kwarg — the emitter supplies the interp form.
        src = ("bot \"B\":\n"
               "  description: \"B\"\n"
               "strings en:\n"
               "  \"hi\": \"Hello {user}!\"\n"
               "command a:\n"
               "  do:\n"
               "    reply t(\"hi\")\n")
        text = emit_js_main_text(src)
        self.assertIn("{user: interaction.user.mention}", text)


class TestBotStore(unittest.TestCase):
    def test_store_methods_on_bot(self):
        text = emit_shop_text()
        for name in ("economy_balance", "economy_credit",
                     "economy_debit", "economy_transfer",
                     "economy_get", "economy_set", "economy_delete"):
            self.assertIn(f"async {name}(", text)
        self.assertIn('new DatabaseSync("economy.db")', text)
        self.assertIn("PRAGMA busy_timeout = 5000", text)

    def test_store_key_from_interpolation(self):
        text = emit_shop_text()
        self.assertIn("`buy-${interaction.user.id}-${item}-${amount}`",
                      text)

    def test_economy_uses_explicit_transactions(self):
        # J9: one explicit BEGIN IMMEDIATE...COMMIT per op (the
        # atomicity is the transaction, not a lock — node:sqlite is
        # synchronous and single-threaded, so no interleaving is
        # possible). Int columns are TEXT, converted with BigInt.
        text = emit_shop_text()
        self.assertIn('"BEGIN IMMEDIATE"', text)
        self.assertIn('"COMMIT"', text)
        self.assertIn('"ROLLBACK"', text)
        self.assertIn("balance TEXT", text)
        self.assertIn("BigInt(row.balance)", text)
        self.assertNotIn("asyncio.Lock", text)
        self.assertNotIn("aiosqlite", text)


class TestBotComponents(unittest.TestCase):
    def test_button_row_and_collector(self):
        text = emit_shop_text()
        self.assertIn("new ActionRowBuilder().addComponents(", text)
        self.assertIn('new ButtonBuilder().setCustomId("buy-again")',
                      text)
        self.assertIn(".setStyle(ButtonStyle.Primary)", text)
        self.assertIn("createMessageComponentCollector", text)
        self.assertIn("ComponentType.Button", text)
        self.assertIn('time: 60000', text)
        # The author gate lives in the collector's 'collect'
        # callback (it closes over the invoking interaction).
        self.assertIn('i.customId === "buy-again" && i.user.id !== interaction.user.id',
                      text)
        self.assertIn('"This button is not for you."', text)
        # on_timeout: disable the row, edit the message, run the body.
        self.assertIn('reason === "time"', text)
        self.assertIn("child.setDisabled(true);", text)
        self.assertIn('await _newr_c0_message.edit({ content: "Offer expired." });',
                      text)
        # The shared handler is registered by customId.
        self.assertIn('["buy-again", async (interaction) => {', text)

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
        text = emit_js_main_text(src)
        self.assertIn("new StringSelectMenuBuilder()", text)
        self.assertIn(".setPlaceholder(\"Pick one\")", text)
        self.assertIn("ComponentType.StringSelect", text)
        self.assertIn("interaction.values", text)

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
        text = emit_js_main_text(src)
        self.assertIn("new ModalBuilder()", text)
        self.assertIn("await interaction.showModal(", text)
        self.assertIn("TextInputStyle.Paragraph", text)
        self.assertIn("interaction.fields.getTextInputValue", text)

    def test_task_loop(self):
        text = emit_shop_text()
        self.assertIn("async function _every_1h() {", text)
        self.assertIn("setInterval(() => { _every_1h()", text)
        self.assertIn(", 3600000);", text)


class TestBotBindings(unittest.TestCase):
    def test_handler_binding_forms(self):
        text = emit_js_main_text(HELLO_BOT)
        self.assertIn("interaction.user.mention", text)
        self.assertIn("interaction.guild ? interaction.guild.name : 'DM'",
                      text)

    def test_member_join_bindings(self):
        text = emit_js_main_text(HELLO_BOT)
        self.assertIn("bot.on(Events.GuildMemberAdd,", text)
        self.assertIn("member.guild.systemChannel", text)
        self.assertIn("member.mention", text)

    def test_ready_bot_binding(self):
        text = emit_js_main_text(HELLO_BOT)
        self.assertIn("bot.once(Events.ClientReady,", text)
        self.assertIn("readyClient.user.tag", text)

    def test_channel_text_only_when_used(self):
        # `_channelText` is emitted only when a `{channel}`
        # interpolation uses it — the shop has none.
        self.assertNotIn("_channelText", emit_shop_text())
        text = emit_js_main_text(
            "bot \"B\":\n"
            "  description: \"B\"\n"
            "command a:\n"
            "  do:\n"
            "    reply \"in {channel}\"\n")
        self.assertIn("function _channelText(channel) {", text)
        self.assertIn("_channelText(interaction.channel)", text)


if __name__ == "__main__":
    unittest.main()

def run_hello_driver(driver_js):
    """Run a driver against the emitted HELLO_BOT."""
    result = emit_js_src(HELLO_BOT, "hello_bot.newr")
    return run_bot_driver(driver_js, result.files)


@unittest.skipUnless(NODE, "node is not installed")
class TestBotMockRoundTrip(unittest.TestCase):
    def test_ping_and_greet_replies(self):
        driver = """
const { bot } = require("./hello_bot.js");
const { Events } = require("discord.js");
const lib = require("./bot-driver-lib.js");
(async () => {
  const ping = lib.makeInteraction({ commandName: "ping", isChatInputCommand: () => true });
  await bot._fire(Events.InteractionCreate, ping);
  const greet = lib.makeInteraction({ commandName: "greet", isChatInputCommand: () => true });
  await bot._fire(Events.InteractionCreate, greet);
  console.log(JSON.stringify({
    ping: ping.replies[0] && ping.replies[0].content,
    greet: greet.replies[0] && greet.replies[0].content,
    greetEphemeral: !!(greet.replies[0] && greet.replies[0].flags === 64),
    commands: [...bot._handlers.keys()],
  }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        out = run_hello_driver(driver)
        self.assertEqual(out["ping"], "Pong! Hello <@42>")
        self.assertEqual(out["greet"], "Hello <@42>, welcome to Testville!")
        self.assertTrue(out["greetEphemeral"])

    def test_greet_in_dm(self):
        driver = """
const { bot } = require("./hello_bot.js");
const { Events } = require("discord.js");
const lib = require("./bot-driver-lib.js");
(async () => {
  const greet = lib.makeInteraction({ commandName: "greet", guild: null, isChatInputCommand: () => true });
  await bot._fire(Events.InteractionCreate, greet);
  console.log(JSON.stringify({ greet: greet.replies[0] && greet.replies[0].content }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        out = run_hello_driver(driver)
        self.assertEqual(out["greet"], "Hello <@42>, welcome to DM!")

    def test_member_join(self):
        driver = """
const { bot } = require("./hello_bot.js");
const { Events } = require("discord.js");
const lib = require("./bot-driver-lib.js");
(async () => {
  const member = lib.makeMember(7, "joiner");
  member.guild.systemChannel = lib.makeChannel();
  await bot._fire(Events.GuildMemberAdd, member);
  console.log(JSON.stringify({ sent: member.guild.systemChannel.sent.map((s) => s.content) }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        out = run_hello_driver(driver)
        self.assertEqual(out["sent"], ["Welcome to Testville, <@7>!"])

    def test_channel_interpolation(self):
        # `{channel}` renders through _channelText (its mention).
        src = ("bot \"B\":\n"
               "  description: \"B\"\n"
               "command a:\n"
               "  do:\n"
               "    reply \"in {channel}\"\n")
        result = emit_js_src(src, "chan_bot.newr")
        driver = """
const { bot } = require("./chan_bot.js");
const { Events } = require("discord.js");
const lib = require("./bot-driver-lib.js");
(async () => {
  const inter = lib.makeInteraction({ commandName: "a", isChatInputCommand: () => true });
  await bot._fire(Events.InteractionCreate, inter);
  console.log(JSON.stringify({ reply: inter.replies[0] && inter.replies[0].content }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        out = run_bot_driver(driver, result.files)
        self.assertEqual(out["reply"], "in #general")

    def test_shop_buy_flow(self):
        # Fund the user, /buy a brick, and check the reply + the
        # balance + the button's author gate + the timeout edit.
        driver = """
const { bot } = require("./shop.js");
const { Events } = require("discord.js");
const lib = require("./bot-driver-lib.js");
(async () => {
  await bot.setup();
  await bot.economy_credit("42", 1000n);
  const buy = lib.makeInteraction({
    commandName: "buy",
    isChatInputCommand: () => true,
    options: {
      getString: (name) => "brick",
      getInteger: (name) => null,
      getNumber: () => null,
      getBoolean: () => null,
      getUser: () => null,
      getMember: () => null,
      getChannel: () => null,
      getRole: () => null,
    },
  });
  await bot._fire(Events.InteractionCreate, buy);
  const reply = buy.replies[0] || {};
  const message = buy._message;
  const collector = message.collectors[0];
  // The author clicks: the handler replies.
  const authorClick = lib.makeInteraction({
    customId: "buy-again",
    isButton: () => true,
  });
  await collector.collect(authorClick);
  // A stranger clicks: refused, no handler reply.
  const strangerClick = lib.makeInteraction({
    customId: "buy-again",
    user: lib.makeUser(99, "stranger"),
    isButton: () => true,
  });
  await collector.collect(strangerClick);
  // Timeout: the message is edited with the expiry.
  await collector.end("time");
  console.log(JSON.stringify({
    reply: reply.content,
    hasComponents: !!(reply.components && reply.components.length === 1),
    balance: String(await bot.economy_balance("42")),
    authorReply: authorClick.replies[0] && authorClick.replies[0].content,
    strangerReply: strangerClick.replies[0] && strangerClick.replies[0].content,
    strangerEphemeral: !!(strangerClick.replies[0] && strangerClick.replies[0].flags === 64),
    timeoutEdits: message.edits.map((e) => e.content || null),
    buttonDisabled: !!(message.edit.length >= 0),
  }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        out = run_shop_driver(driver)
        self.assertEqual(out["reply"],
                         "Bought brick for 118 gold. Balance: 882.")
        self.assertTrue(out["hasComponents"])
        self.assertEqual(out["balance"], "882")
        self.assertEqual(out["authorReply"], "Use /buy again, <@42>!")
        self.assertEqual(out["strangerReply"], "This button is not for you.")
        self.assertTrue(out["strangerEphemeral"])
        self.assertIn("Offer expired.", out["timeoutEdits"])

    def test_shop_buy_poor(self):
        driver = """
const { bot } = require("./shop.js");
const { Events } = require("discord.js");
const lib = require("./bot-driver-lib.js");
(async () => {
  await bot.setup();
  const buy = lib.makeInteraction({
    commandName: "buy",
    isChatInputCommand: () => true,
    options: {
      getString: (name) => "potion",
      getInteger: (name) => 2,
      getNumber: () => null,
      getBoolean: () => null,
      getUser: () => null,
      getMember: () => null,
      getChannel: () => null,
      getRole: () => null,
    },
  });
  await bot._fire(Events.InteractionCreate, buy);
  console.log(JSON.stringify({
    reply: buy.replies[0] && buy.replies[0].content,
    ephemeral: !!(buy.replies[0] && buy.replies[0].flags === 64),
    balance: String(await bot.economy_balance("42")),
  }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        out = run_shop_driver(driver)
        self.assertEqual(out["reply"],
                         "Not enough gold — potion costs 500, you have 0.")
        self.assertTrue(out["ephemeral"])
        self.assertEqual(out["balance"], "0")

    def test_shop_balance_command(self):
        driver = """
const { bot } = require("./shop.js");
const { Events } = require("discord.js");
const lib = require("./bot-driver-lib.js");
(async () => {
  await bot.setup();
  await bot.economy_credit("42", 250n);
  const bal = lib.makeInteraction({ commandName: "balance", isChatInputCommand: () => true });
  await bot._fire(Events.InteractionCreate, bal);
  console.log(JSON.stringify({ reply: bal.replies[0] && bal.replies[0].content }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        out = run_shop_driver(driver)
        self.assertEqual(out["reply"], "Balance: 250 gold.")

    def test_transfer_fresh_replay_and_insufficient(self):
        driver = """
const { bot } = require("./shop.js");
(async () => {
  await bot.setup();
  await bot.economy_credit("alice", 100n);
  const ok = await bot.economy_transfer("alice", "bob", 50n, "K1");
  const replay = await bot.economy_transfer("alice", "bob", 50n, "K1");
  const poor = await bot.economy_transfer("alice", "bob", 100n, "K2");
  console.log(JSON.stringify({
    ok,
    replay,
    poor,
    alice: String(await bot.economy_balance("alice")),
    bob: String(await bot.economy_balance("bob")),
  }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        out = run_shop_driver(driver)
        self.assertTrue(out["ok"])
        self.assertTrue(out["replay"])
        self.assertFalse(out["poor"])
        self.assertEqual(out["alice"], "50")
        self.assertEqual(out["bob"], "50")

    def test_transfer_with_out_leg_poisoned_then_retry_heals(self):
        # Seed alice 100, simulate a crash between the two legs
        # (out-leg committed, in-leg never ran), then retry — the
        # retry must HEAL the transfer (credit bob the missing 50),
        # not report success on a half-done transfer. Total money
        # is conserved (100).
        driver = """
const { bot } = require("./shop.js");
(async () => {
  await bot.setup();
  await bot.economy_credit("alice", 100n);
  bot.economy_db.prepare("INSERT INTO balances (user, balance) VALUES (?, ?) ON CONFLICT(user) DO UPDATE SET balance = balance + excluded.balance").run("alice", "-50");
  bot.economy_db.prepare("INSERT INTO ledger (user, amount, key, created_at) VALUES (?, ?, ?, ?)").run("alice", "-50", "K:out", 0);
  const healed = await bot.economy_transfer("alice", "bob", 50n, "K");
  const again = await bot.economy_transfer("alice", "bob", 50n, "K");
  console.log(JSON.stringify({
    healed,
    again,
    alice: String(await bot.economy_balance("alice")),
    bob: String(await bot.economy_balance("bob")),
  }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        out = run_shop_driver(driver)
        self.assertTrue(out["healed"])
        self.assertTrue(out["again"])
        self.assertEqual(out["alice"], "50")
        self.assertEqual(out["bob"], "50")

    def test_transfer_with_in_leg_poisoned_then_retry_heals(self):
        # The other poisoning: a synthetic `K:in` row (amount 0)
        # with no money moved. The retry must still move the full
        # 50 in both legs.
        driver = """
const { bot } = require("./shop.js");
(async () => {
  await bot.setup();
  await bot.economy_credit("alice", 100n);
  bot.economy_db.prepare("INSERT INTO ledger (user, amount, key, created_at) VALUES (?, ?, ?, ?)").run("bob", "0", "K:in", 0);
  const healed = await bot.economy_transfer("alice", "bob", 50n, "K");
  const again = await bot.economy_transfer("alice", "bob", 50n, "K");
  console.log(JSON.stringify({
    healed,
    again,
    alice: String(await bot.economy_balance("alice")),
    bob: String(await bot.economy_balance("bob")),
  }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        out = run_shop_driver(driver)
        self.assertTrue(out["healed"])
        self.assertTrue(out["again"])
        self.assertEqual(out["alice"], "50")
        self.assertEqual(out["bob"], "50")

    def test_concurrent_debits_cannot_overdraw(self):
        # Two concurrent debits of 60 against a balance of 100:
        # exactly one succeeds; the balance ends at 40, never -20.
        # (In JS the sqlite calls are synchronous, so the
        # transaction is atomic by construction — no lock is needed;
        # the test pins the outcome, not the mechanism.)
        driver = """
const { bot } = require("./shop.js");
(async () => {
  await bot.setup();
  await bot.economy_credit("alice", 100n);
  const results = await Promise.all([
    bot.economy_debit("alice", 60n, "D1"),
    bot.economy_debit("alice", 60n, "D2"),
  ]);
  console.log(JSON.stringify({
    results: results.slice().sort(),
    alice: String(await bot.economy_balance("alice")),
  }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        out = run_shop_driver(driver)
        self.assertEqual(out["results"], [False, True])
        self.assertEqual(out["alice"], "40")

    def test_credit_idempotent_on_key(self):
        driver = """
const { bot } = require("./shop.js");
(async () => {
  await bot.setup();
  await bot.economy_credit("alice", 100n, "C1");
  await bot.economy_credit("alice", 100n, "C1");
  console.log(JSON.stringify({ alice: String(await bot.economy_balance("alice")) }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        out = run_shop_driver(driver)
        self.assertEqual(out["alice"], "100")

    def test_debit_replay_returns_true_without_double_debit(self):
        driver = """
const { bot } = require("./shop.js");
(async () => {
  await bot.setup();
  await bot.economy_credit("alice", 100n);
  const first = await bot.economy_debit("alice", 30n, "D1");
  const replay = await bot.economy_debit("alice", 30n, "D1");
  console.log(JSON.stringify({
    first,
    replay,
    alice: String(await bot.economy_balance("alice")),
  }));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""
        out = run_shop_driver(driver)
        self.assertTrue(out["first"])
        self.assertTrue(out["replay"])
        self.assertEqual(out["alice"], "70")
