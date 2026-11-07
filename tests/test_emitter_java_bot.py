"""Tests for the Java bot emitter (compiler/emitter_java.py, M4 bot phase).

The Java twin of tests/test_emitter_js_bot.py: the same bot programs
(HELLO_BOT, SHOP) must emit JDA 6.7.0 code with the same semantics as
the Python/JS targets - slash commands, a store economy with SQLite
+ idempotency, buttons/selects/modals, reply/defer/edit/followup/send,
a standalone `<stem>_deploy.java` registration program, and a pom.xml
for bot programs.

Text-assertion tests run in the normal suite (they need no JDA).
Compile tests run javac over the emitted files with the JDA +
sqlite-jdbc jars on the classpath; the jars are found in
$NEWR_JAVA_JARS, ~/jars, or tests/java/lib, else downloaded once
from Maven Central into ~/.cache/newr-java-test-libs - and the
compile tests skip (they do not fail) when no network and no jars
are available. No live Discord is ever touched.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

from compiler.checker import check_program
from compiler.discord_check import check_discord, discord_providers
from compiler.emitter_java import EmitterError, emit_program as emit_java
from compiler.resolver import resolve_file
from tests.test_emitter_java import (
    JAVA, JAVAC, RT_JAVA_SRC, emit_java_main_text, emit_java_src,
    full_program,
)

ROOT = Path(__file__).resolve().parent.parent

JDA_VERSION = "6.7.0"
SQLITE_JDBC_VERSION = "3.54.1.0"
JDA_JAR = f"jda-{JDA_VERSION}.jar"
SQLITE_JAR = f"sqlite-jdbc-{SQLITE_JDBC_VERSION}.jar"
_JAR_URLS = {
    JDA_JAR: ("https://repo.maven.apache.org/maven2/net/dv8tion/JDA/"
              f"{JDA_VERSION}/{JDA_JAR}"),
    SQLITE_JAR: ("https://repo.maven.apache.org/maven2/org/xerial/"
                 f"sqlite-jdbc/{SQLITE_JDBC_VERSION}/{SQLITE_JAR}"),
}

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
  "buy.poor": "Not enough gold - {item} costs {price}, you have {balance}."
strings id:
  "buy.ok": "Membeli {item} seharga {price} gold. Saldo: {balance}."
  "buy.poor": "Gold tidak cukup - {item} harganya {price}, kamu punya {balance}."

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
        return emit_java(resolved.program, main_path=str(main),
                         sources=sources, output_stem="shop")


def emit_shop_text():
    """Emit SHOP from a real temp dir; the main file's text."""
    result = _emit_shop_result()
    return result.files[result.main_file]


def _find_jars():
    """Locate the JDA + sqlite-jdbc jars, or download them once into
    ~/.cache/newr-java-test-libs. Returns (jda_path, sqlite_path) or
    (None, None) when neither local jars nor the network are there."""
    candidates = []
    if os.environ.get("NEWR_JAVA_JARS"):
        candidates.append(Path(os.environ["NEWR_JAVA_JARS"]))
    candidates.append(Path.home() / "jars")
    candidates.append(ROOT / "tests" / "java" / "lib")
    for d in candidates:
        jda, lite = d / JDA_JAR, d / SQLITE_JAR
        if jda.is_file() and lite.is_file():
            return str(jda), str(lite)
    cache = Path.home() / ".cache" / "newr-java-test-libs"
    cache.mkdir(parents=True, exist_ok=True)
    try:
        for name, url in _JAR_URLS.items():
            dest = cache / name
            if not dest.is_file():
                urllib.request.urlretrieve(url, dest)
    except Exception:
        return None, None
    jda, lite = cache / JDA_JAR, cache / SQLITE_JAR
    if jda.is_file() and lite.is_file():
        return str(jda), str(lite)
    return None, None


_JDA_JAR, _SQLITE_JAR = _find_jars()
_HAVE_JARS = _JDA_JAR is not None and _SQLITE_JAR is not None


def compile_java_result(result):
    """Write an EmitResult (+ NewrRt.java when used) to a temp dir
    and compile every .java file with the JDA + sqlite-jdbc jars on
    the classpath. Returns the javac CompletedProcess."""
    with tempfile.TemporaryDirectory() as d:
        for name, text in result.files.items():
            (Path(d) / name).write_text(text, encoding="utf-8")
        if result.uses_rt:
            (Path(d) / "NewrRt.java").write_text(
                RT_JAVA_SRC.read_text(encoding="utf-8"), encoding="utf-8")
        java_files = [str(p) for p in Path(d).glob("*.java")]
        cp = os.pathsep.join([_JDA_JAR, _SQLITE_JAR])
        return subprocess.run(
            [JAVAC, "-cp", cp, *java_files],
            capture_output=True, text=True, cwd=d)


def assert_compiles(testcase, result):
    proc = compile_java_result(result)
    testcase.assertEqual(proc.returncode, 0,
                         f"javac failed:\n{proc.stderr}")


@unittest.skipUnless(JAVAC, "a JDK is not installed")
class TestHelloBot(unittest.TestCase):
    def test_hello_bot_shape(self):
        text = emit_java_main_text(HELLO_BOT, "hello_bot.newr", "hello_bot")
        self.assertIn("public class hello_bot extends ListenerAdapter", text)
        self.assertIn("JDABuilder.createDefault(_newrToken)", text)
        self.assertIn('Commands.slash("ping", "Answer with pong")', text)
        self.assertIn('Commands.slash("greet", "Greet someone in this server")',
                      text)
        self.assertIn("setEphemeral(true)", text)
        self.assertIn("onSlashCommandInteraction", text)
        self.assertIn("onReady", text)
        self.assertIn("onGuildMemberJoin", text)
        self.assertIn("interaction.getUser().getAsMention()", text)
        self.assertIn("getSystemChannel()", text)
        result = emit_java_src(HELLO_BOT, "hello_bot.newr", "hello_bot")
        self.assertIn("hello_bot_deploy.java", result.files)
        self.assertIn("pom.xml", result.files)
        self.assertIn("<artifactId>JDA</artifactId>", result.files["pom.xml"])
        self.assertIn(f"<version>{JDA_VERSION}</version>",
                      result.files["pom.xml"])
        self.assertNotIn("sqlite-jdbc", result.files["pom.xml"])

    def test_hello_bot_deploy_shape(self):
        result = emit_java_src(HELLO_BOT, "hello_bot.newr", "hello_bot")
        deploy = result.files["hello_bot_deploy.java"]
        self.assertIn("public class hello_bot_deploy", deploy)
        self.assertIn("DISCORD_TOKEN", deploy)
        self.assertIn("DISCORD_CLIENT_ID", deploy)
        self.assertIn("DISCORD_GUILD_ID", deploy)
        self.assertIn("hello_bot.COMMANDS", deploy)
        self.assertIn("updateCommands().addCommands(", deploy)

    @unittest.skipUnless(_HAVE_JARS, "JDA/sqlite-jdbc jars unavailable")
    def test_hello_bot_compiles(self):
        result = emit_java_src(HELLO_BOT, "hello_bot.newr", "hello_bot")
        assert_compiles(self, result)


@unittest.skipUnless(JAVAC, "a JDK is not installed")
class TestShop(unittest.TestCase):
    def test_shop_shape(self):
        text = emit_shop_text()
        # Commands + options (JDA builders, not discord.js ones).
        self.assertIn('Commands.slash("buy", "Buy an item from the shop")',
                      text)
        self.assertIn("OptionType.STRING", text)
        self.assertIn("OptionType.INTEGER", text)
        self.assertIn('addChoice("brick", "brick")', text)
        self.assertIn("setMinValue(1L)", text)
        self.assertIn("setMaxValue(99L)", text)
        self.assertIn("getAsString()", text)
        self.assertIn("getAsLong()", text)
        # Strings + _t (D16).
        self.assertIn("_DEFAULT_LOCALE", text)
        self.assertIn('"buy.ok"', text)
        self.assertIn("private static String _t(", text)
        # Store: JDBC, one long-lived connection, explicit
        # transactions, ledger + idempotency (P11).
        self.assertIn("DriverManager.getConnection(\"jdbc:sqlite:economy.db\")",
                      text)
        self.assertIn("BEGIN IMMEDIATE", text)
        self.assertIn("ROLLBACK", text)
        self.assertIn("INSERT INTO ledger", text)
        self.assertIn("economy_balance", text)
        self.assertIn("economy_credit", text)
        self.assertIn("bot.economy_balance(", text)
        self.assertIn("bot.economy_credit(", text)
        # Button: ActionRow + Button.primary, author gate, timeout.
        self.assertIn("Button.primary(\"buy-again\", \"Buy again\")", text)
        self.assertIn("ActionRow.of(", text)
        self.assertIn("buyAgainHandler", text)
        self.assertIn("This button is not for you.", text)
        self.assertIn("Offer expired.", text)
        # Task: scheduled, not setInterval.
        self.assertIn("_every_1h", text)
        self.assertIn("scheduleAtFixedRate", text)
        # Deploy + pom.
        result = _emit_shop_result()
        self.assertIn("shop_deploy.java", result.files)
        self.assertIn("pom.xml", result.files)
        self.assertIn("sqlite-jdbc", result.files["pom.xml"])
        self.assertIn(f"<version>{SQLITE_JDBC_VERSION}</version>",
                      result.files["pom.xml"])

    @unittest.skipUnless(_HAVE_JARS, "JDA/sqlite-jdbc jars unavailable")
    def test_shop_compiles(self):
        assert_compiles(self, _emit_shop_result())


class TestBotStatements(unittest.TestCase):
    def test_store_call_outside_handler_is_rejected(self):
        # §19: a store call lowers to `bot.<store>_<op>`, which only
        # exists inside a handler. At the top level, inside a (sync)
        # top-level fn, or inside a lambda there is no handler
        # context - a clean E4001, never uncompilable code.
        store = (
            "store economy:\n"
            "  table balances (user: str, balance: int)\n"
            "  ledger true\n\n"
        )
        bot = (
            'bot "B":\n'
            '  description: "B"\n\n'
            'command ping:\n'
            '  description: "Ping"\n'
            '  do:\n'
            '    reply "pong"\n'
        )
        # Top-level statement.
        with self.assertRaises(EmitterError):
            emit_java_src(store + 'let b: int = economy.balance("alice")\n\n'
                          + bot, "<test>.newr", "Main")
        # Top-level fn.
        with self.assertRaises(EmitterError):
            emit_java_src(store +
                          "fn bal() -> int:\n"
                          '  return economy.balance("alice")\n\n' + bot,
                          "<test>.newr", "Main")
        # Lambda inside a handler.
        lam = (
            'bot "B":\n'
            '  description: "B"\n\n'
            'command ping:\n'
            '  description: "Ping"\n'
            '  do:\n'
            '    let f = fn() -> int:\n'
            '      return economy.balance("alice")\n'
            '    reply "done"\n'
        )
        with self.assertRaises(EmitterError):
            emit_java_src(store + lam, "<test>.newr", "Main")

    def test_reply_defer_edit_followup_send(self):
        # `send` is only allowed in a message event or a task body
        # (E2008), so the send form is tested in a message event.
        src = (
            "bot \"B\":\n  description: \"b\"\n"
            "  intents: [message_content]\n"
            "command x:\n  do:\n"
            "    defer\n"
            "    reply \"first\"\n"
            "    edit \"second\"\n"
            "    followup \"third\"\n"
            "event message:\n  do:\n"
            "    send \"hello\" to message.channel\n")
        text = emit_java_main_text(src, "<test>.newr", "Main")
        self.assertIn("interaction.deferReply().complete();", text)
        self.assertIn('interaction.reply("first")', text)
        self.assertIn('interaction.getHook().editOriginal("second")', text)
        self.assertIn('interaction.getHook().sendMessage("third")', text)
        self.assertIn('event.getMessage().getChannel().sendMessage("hello")', text)

    def test_select_and_modal_emit(self):
        src = (
            "bot \"B\":\n  description: \"b\"\n\n"
            "command pick:\n  description: \"p\"\n  do:\n"
            "    reply \"choose\"\n"
            "    select \"Pick one\" (options: [\"a\", \"b\"], timeout: 30s):\n"
            "      only: author\n"
            "      do:\n"
            "        reply \"you picked {\", \".join(values)}\"\n"
            "    modal \"Feedback\" (timeout: 120s):\n"
            "      text_input \"name\" (style: short, required):\n"
            "      text_input \"notes\" (style: paragraph):\n"
            "      do:\n"
            "        reply \"thanks {name}\"\n")
        text = emit_java_main_text(src, "<test>.newr", "Main")
        self.assertIn("StringSelectMenu.create(", text)
        self.assertIn("SelectOption.of(\"a\", \"a\")", text)
        self.assertIn("interaction.getValues()", text)
        self.assertIn("Modal.create(", text)
        self.assertIn("TextInput.create(", text)
        self.assertIn("TextInputStyle.PARAGRAPH", text)
        self.assertIn("replyModal(", text)
        self.assertIn('interaction.getValue("name").getAsString()', text)

    def test_store_get_row_compiles(self):
        # A `get` returns the table's row type (a struct-like type
        # named after the table); field access on it must compile.
        src = (
            "bot \"B\":\n  description: \"b\"\n"
            "store economy:\n"
            "  table balances (user: str, balance: int)\n"
            "  ledger true\n\n"
            "command c:\n  description: \"c\"\n  do:\n"
            "    let v = economy.get(\"balances\", \"u1\")?\n"
            "    let b: int = v.balance\n"
            "    reply \"bal {b}\"\n")
        result = emit_java_src(src, "<test>.newr", "Main")
        text = result.files[result.main_file]
        self.assertIn("class balances", text)
        self.assertIn("economy_get(", text)
        if _HAVE_JARS and JAVAC:
            assert_compiles(self, result)

    def test_ephemeral_only_on_interaction_reply(self):
        # `ephemeral` on a member-join reply is a clean E4001 (the
        # checker allows it; the emitter refuses - only an
        # interaction reply can be ephemeral).
        src = (
            "bot \"B\":\n  description: \"b\"\n\n"
            "event member_join:\n  do:\n"
            "    reply \"hi {user}\" ephemeral\n")
        with self.assertRaises(EmitterError):
            emit_java_src(src, "<test>.newr", "Main")


if __name__ == "__main__":
    unittest.main()
