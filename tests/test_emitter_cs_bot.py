"""Tests for the C# Discord.Net bot emitter (compiler/emitter_cs.py, M4).

The C# twin of tests/test_emitter_js_bot.py: the same bot programs
(HELLO_BOT, SHOP) must emit Discord.Net 3.20.2 code with the same
shape and behaviour as the Python/JS targets — a nested `Bot :
DiscordSocketClient`, slash commands as a `Commands` list of
(SlashCommandBuilder, handler) tuples, a `ComponentHandlers` /
`ModalHandlers` map, a standalone `<stem>_deploy.cs` (DiscordRestClient
+ BulkOverwrite), store methods on Bot with the same ledger +
idempotency + heal semantics (Microsoft.Data.Sqlite, one long-lived
connection, one transaction per mutation).

Text-assertion tests run in the normal suite (they need no packages).
Compile tests build the emitted code with dotnet (Discord.Net 3.20.2
+ Microsoft.Data.Sqlite 8.0.0, both verified live on the build VM);
the store harness drives the emitted Bot class directly against a
real SQLite file — no live Discord connection anywhere in this file.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from compiler.checker import check_program
from compiler.discord_check import check_discord, discord_providers
from compiler.emitter_cs import EmitterError, emit_program as emit_cs
from compiler.resolver import resolve_file
from tests.test_emitter_cs import (
    DOTNET, RT_CS_SRC, emit_cs_main_text, emit_cs_src, full_program,
)

ROOT = Path(__file__).resolve().parent.parent

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


def emit_cs_bot_text(src, filename="hello_bot.newr", stem="hello_bot"):
    """Emit a bot source string; the main file's text."""
    return emit_cs_main_text(src, filename, stem)


def _emit_shop_result():
    """Emit SHOP from a real temp dir so its `import "shop_items.newr"`
    resolves (the shop_items source is copied from examples/).
    Returns the full EmitResult (all emitted files)."""
    items = (ROOT / "examples" / "shop_items.newr").read_text(
        encoding="utf-8")
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
        return emit_cs(resolved.program, main_path=str(main),
                       sources=sources, output_stem="shop")


def emit_shop_text():
    """Emit SHOP from a real temp dir; the main file's text."""
    result = _emit_shop_result()
    return result.files[result.main_file]


def _dotnet_env():
    env = dict(os.environ)
    env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
    # /tmp is a 512MB tmpfs — keep dotnet temp dirs on the home disk.
    # Fresh CI runners have no ~/dotnet-tmp yet; create it first
    # (same pattern as compiler/app_pack_cs.py _dotnet_env).
    (Path.home() / "dotnet-tmp").mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(Path.home() / "dotnet-tmp")
    return env


def build_files(files, csproj_text, startup=None):
    """Write `files` + a csproj into a temp dir and `dotnet build` it.

    Returns (returncode, stdout, stderr, tempdir-as-Path) — the caller
    owns the temp dir (returned so a second command, e.g. `dotnet
    run`, can reuse it). Use `shutil.rmtree` when done.
    """
    # mkdtemp() does not create a missing parent: on a fresh CI
    # runner ~/dotnet-tmp does not exist yet and all five bot-compile
    # tests error before any compilation runs (run 40, 5 ERRORs).
    (Path.home() / "dotnet-tmp").mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=str(Path.home() / "dotnet-tmp")))
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")
    # D4's C# twin: the runtime is copied next to the output when
    # any emitted file uses it (the shop's row structs implement
    # INewrRepr and call NewrRt.* helpers).
    if any("NewrRt" in t or "INewrRepr" in t for t in files.values()):
        (d / "newr_rt.cs").write_text(
            RT_CS_SRC.read_text(encoding="utf-8"), encoding="utf-8")
    (d / "build.csproj").write_text(csproj_text, encoding="utf-8")
    args = [DOTNET, "build", str(d / "build.csproj")]
    if startup:
        args += ["-p:StartupObject=" + startup]
    proc = subprocess.run(args, capture_output=True, text=True, cwd=d,
                          env=_dotnet_env())
    return proc.returncode, proc.stdout, proc.stderr, d


def _bot_csproj(stem, startup=None):
    lines = [
        "<Project Sdk=\"Microsoft.NET.Sdk\">",
        "  <PropertyGroup>",
        "    <OutputType>Exe</OutputType>",
        "    <TargetFramework>net8.0</TargetFramework>",
        "    <Nullable>disable</Nullable>",
        "    <ImplicitUsings>disable</ImplicitUsings>",
        "    <NoWarn>CS8981</NoWarn>",
        f"    <AssemblyName>{stem}</AssemblyName>",
    ]
    if startup:
        lines.append(f"    <StartupObject>{startup}</StartupObject>")
    lines += [
        "  </PropertyGroup>",
        "  <ItemGroup>",
        "    <PackageReference Include=\"Discord.Net\" Version=\"3.20.2\" />",
        "    <PackageReference Include=\"Microsoft.Data.Sqlite\" "
        "Version=\"8.0.0\" />",
        "  </ItemGroup>",
    ]
    if not startup:
        # A bot build excludes its deploy file (it has its own
        # Main). A deploy scratch build (startup given) compiles
        # BOTH files and lets <StartupObject> pick the deploy one.
        lines += [
            "  <ItemGroup>",
            "    <Compile Remove=\"*_deploy.cs\" />",
            "  </ItemGroup>",
        ]
    lines += [
        "</Project>",
        "",
    ]
    return "\n".join(lines)


def assert_builds(testcase, files, stem, extra_files=None,
                  startup=None):
    """Build `files` (an EmitResult.files dict) with the bot csproj;
    assert the build succeeds, and return the temp dir for a later
    `dotnet run` (the caller removes it)."""
    all_files = dict(files)
    all_files.pop(stem + ".csproj", None)
    for name, text in (extra_files or {}).items():
        all_files[name] = text
    code, out, err, d = build_files(all_files, _bot_csproj(stem, startup),
                                    startup=startup)
    testcase.assertEqual(code, 0, f"dotnet build failed\n{out}\n{err}")
    return d


@unittest.skipUnless(DOTNET, "dotnet is not installed")
class TestBotStructure(unittest.TestCase):
    def test_hello_bot_shape(self):
        text = emit_cs_bot_text(HELLO_BOT)
        self.assertIn("public class Bot : DiscordSocketClient", text)
        self.assertIn("public static Bot bot;", text)
        self.assertIn("Commands", text)
        self.assertIn("SlashCommandBuilder", text)
        self.assertIn(".WithName(\"ping\")", text)
        self.assertIn(".WithName(\"greet\")", text)
        # Registration is a separate file, never on ready.
        self.assertNotIn("BulkOverwrite", text)
        self.assertNotIn("CreateGlobalCommand", text)

    def test_deploy_file_is_standalone_rest(self):
        result = emit_cs_src(HELLO_BOT, "hello_bot.newr", "hello_bot")
        self.assertIn("hello_bot_deploy.cs", result.files)
        deploy = result.files["hello_bot_deploy.cs"]
        self.assertIn("DiscordRestClient", deploy)
        self.assertIn("BulkOverwriteGuildCommands", deploy)
        self.assertIn("BulkOverwriteGlobalCommands", deploy)
        self.assertIn("DISCORD_GUILD_ID", deploy)
        self.assertIn("hello_bot.Commands", deploy)
        # The deploy file's header names the .newr source (like
        # every other emitted file), not the deploy file itself.
        self.assertIn("examples/hello_bot.newr"
                      if False else "hello_bot.newr", deploy)
        self.assertNotIn("hello_bot_deploy.cs", deploy.splitlines()[0])

    def test_csproj_has_discord_and_sqlite_packages(self):
        result = emit_cs_src(HELLO_BOT, "hello_bot.newr", "hello_bot")
        csproj = result.files["hello_bot.csproj"]
        self.assertIn(
            "<PackageReference Include=\"Discord.Net\" "
            "Version=\"3.20.2\" />", csproj)
        self.assertIn(
            "<PackageReference Include=\"Microsoft.Data.Sqlite\" "
            "Version=\"8.0.0\" />", csproj)
        # The deploy file must not break the bot build (it has its
        # own Main): the bot project enumerates its Compile items
        # explicitly, so the deploy file is never listed (and a
        # sibling program's .cs files in a shared directory are
        # not globbed in either).
        self.assertIn('<Compile Include="hello_bot.cs" />', csproj)
        self.assertNotIn("hello_bot_deploy.cs", csproj)

    def test_general_csproj_stays_package_free(self):
        result = emit_cs_src("print \"hi\"\n", "<test>.newr", "Main")
        self.assertNotIn("PackageReference",
                       result.files["MainProgram.csproj"])

    def test_intents_from_declaration_and_events(self):
        text = emit_cs_bot_text(HELLO_BOT)
        # member_join adds the members intent (the v0 rule).
        self.assertIn("GatewayIntents.GuildMembers", text)
        shop = emit_shop_text()
        self.assertIn("GatewayIntents.Guilds", shop)
        self.assertIn("GatewayIntents.GuildMembers", shop)

    def test_command_functions_and_ephemeral(self):
        text = emit_cs_bot_text(HELLO_BOT)
        self.assertIn("RespondAsync", text)
        self.assertIn("interaction.User.Mention", text)
        self.assertIn("ephemeral: true", text)
        # A greet in a DM names the guild "DM" (the JS golden's
        # fallback, in C# spelling).
        self.assertIn('"DM"', text)

    def test_no_commands_no_error_handler(self):
        src = ('bot "B":\n  description: "x"\n\n'
               'event ready:\n  log: "ready"\n')
        text = emit_cs_bot_text(src, "<test>.newr", "b")
        self.assertNotIn("_DispatchCommand", text)
        self.assertNotIn("SlashCommandExecuted", text)

    def test_reply_defer_edit_followup_send(self):
        src = ('bot "B":\n  description: "x"\n\n'
               'command c:\n  description: "c"\n  do:\n'
               '    defer\n'
               '    reply "first"\n'
               '    edit "second"\n'
               '    followup "third"\n'
               'event message:\n  do:\n'
               '    send "hi" to message.channel\n')
        text = emit_cs_bot_text(src, "<test>.newr", "b")
        self.assertIn("await interaction.DeferAsync();", text)
        self.assertIn("await interaction.RespondAsync(", text)
        self.assertIn("ModifyOriginalResponseAsync(", text)
        self.assertIn("await interaction.FollowupAsync(", text)
        self.assertIn("await message.Channel.SendMessageAsync(", text)

    def test_embed_reply(self):
        src = ('bot "B":\n  description: "x"\n\n'
               'command c:\n  description: "c"\n  do:\n'
               '    reply embed:\n'
               '      title: "T"\n'
               '      description: "D"\n'
               '      field "f": "v"\n')
        text = emit_cs_bot_text(src, "<test>.newr", "b")
        self.assertIn("new EmbedBuilder()", text)
        self.assertIn('.WithTitle("T")', text)
        self.assertIn('.WithDescription("D")', text)
        self.assertIn('.AddField("f", "v", false)', text)
        self.assertIn("RespondAsync(embed:", text)
        self.assertNotIn("net.dv8tion", text)

    def test_log_levels_in_bot(self):
        src = ('bot "B":\n  description: "x"\n\n'
               'command c:\n  description: "c"\n  do:\n'
               '    log "info"\n'
               '    log.warn("warn")\n'
               '    log.error("err")\n'
               '    reply "ok"\n')
        text = emit_cs_bot_text(src, "<test>.newr", "b")
        self.assertIn('Console.WriteLine("info");', text)
        self.assertIn('Console.Error.WriteLine("warn");', text)
        self.assertIn('Console.Error.WriteLine("err");', text)

    def test_t_call_and_strings_dict(self):
        text = emit_shop_text()
        self.assertIn("_DEFAULT_LOCALE", text)
        self.assertIn("STRINGS", text)
        self.assertIn('"buy.ok"', text)
        self.assertIn('"buy.poor"', text)
        self.assertIn('_t("buy.ok", interaction.UserLocale,', text)
        self.assertIn("NewrRt.DictOf<object>", text)

    def test_store_methods_on_bot(self):
        text = emit_shop_text()
        for name in ("economy__balance", "economy__credit",
                     "economy__debit", "economy__transfer",
                     "economy__get", "economy__set", "economy__delete"):
            self.assertIn(name, text)
        self.assertIn("SqliteConnection", text)
        self.assertIn('"Data Source=economy_.db"', text)
        self.assertIn("Microsoft.Data.Sqlite", text)

    def test_economy_uses_explicit_transactions(self):
        text = emit_shop_text()
        self.assertIn('"BEGIN IMMEDIATE"', text)
        self.assertIn('"COMMIT"', text)
        self.assertIn('"ROLLBACK"', text)
        self.assertIn("ledger", text)
        # Idempotency: a duplicate key is a replay, not an error.
        self.assertIn("SELECT 1 FROM ledger WHERE key = $key", text)

    def test_store_call_outside_handler_is_rejected(self):
        src = ('bot "B":\n  description: "x"\n\n'
               'store economy:\n  table balances (user: str, balance: int)\n'
               '  ledger true\n\n'
               'fn f() -> int:\n  return economy.balance("u1")\n')
        with self.assertRaises(EmitterError):
            emit_cs_src(src, "<test>.newr", "b")

    def test_button_row_and_collector(self):
        text = emit_shop_text()
        self.assertIn("ComponentBuilder", text)
        self.assertIn('.WithButton("Buy again", "buy-again", '
                      'ButtonStyle.Primary)', text)
        self.assertIn("ComponentHandlers", text)
        self.assertIn('"buy-again"', text)
        self.assertIn("GetOriginalResponseAsync", text)
        self.assertIn("_ComponentTimeoutAsync", text)
        self.assertIn("IsDisabled = true", text)
        self.assertIn('"Offer expired."', text)

    def test_select(self):
        src = ('bot "B":\n  description: "x"\n\n'
               'command pick:\n  description: "p"\n  do:\n'
               '    reply "choose"\n'
               '    select "Pick one" (options: ["a", "b"], timeout: 60s):\n'
               '      do:\n'
               '        let n: int = len(values)\n'
               '        reply "picked {n}"\n')
        text = emit_cs_bot_text(src, "<test>.newr", "b")
        self.assertIn(".WithSelectMenu(", text)
        self.assertIn('.WithLabel("a")', text)
        self.assertIn('"Pick one", minValues: 1, maxValues: 1)', text)
        self.assertIn("interaction.Data.Values", text)
        self.assertIn("SelectMenuExecuted", text)

    def test_modal(self):
        src = ('bot "B":\n  description: "x"\n\n'
               'command form:\n  description: "f"\n  do:\n'
               '    modal "Feedback" (id: "feedback", timeout: 120s):\n'
               '      text_input "name" (style: short, required):\n'
               '      text_input "notes" (style: paragraph):\n'
               '      do:\n'
               '        reply "thanks {name}"\n')
        text = emit_cs_bot_text(src, "<test>.newr", "b")
        self.assertIn("ModalBuilder", text)
        self.assertIn('WithCustomId("feedback")', text)
        self.assertIn("RespondWithModalAsync", text)
        self.assertIn("ModalHandlers", text)
        self.assertIn("_ModalValue(interaction,", text)
        self.assertIn("TextInputStyle.Short", text)
        self.assertIn("TextInputStyle.Paragraph", text)

    def test_task_loop(self):
        text = emit_shop_text()
        self.assertIn("_every_1h", text)
        self.assertIn("Task.Delay(3600000)", text)
        self.assertIn("Task.Run", text)

    def test_handler_binding_forms(self):
        src = ('bot "B":\n  description: "x"\n\n'
               'command c:\n  description: "c"\n  do:\n'
               '    reply "{user.id} {user.name} {guild.id} '
               '{guild.name} {guild.member_count} {channel.id}"\n')
        text = emit_cs_bot_text(src, "<test>.newr", "b")
        self.assertIn("(long) (interaction.User.Id)", text)
        self.assertIn("interaction.User.Username", text)
        self.assertIn("(long) ((interaction.Channel as "
                      "SocketGuildChannel)?.Guild.Id)", text)
        self.assertIn("(interaction.Channel as SocketGuildChannel)"
                      "?.Guild.Name", text)
        self.assertIn("MemberCount", text)

    def test_member_join_bindings(self):
        text = emit_cs_bot_text(HELLO_BOT)
        self.assertIn("member.Guild.SystemChannel", text)
        self.assertIn("member.Mention", text)
        self.assertIn("member.Guild.Name", text)
        self.assertIn("UserJoined", text)

    def test_ready_bot_binding(self):
        text = emit_cs_bot_text(HELLO_BOT)
        self.assertIn("bot.CurrentUser.Username", text)
        self.assertIn("Ready +=", text)

    def test_channel_text_only_when_used(self):
        text = emit_cs_bot_text(HELLO_BOT)
        self.assertNotIn("_ChannelText", text)
        src = ('bot "B":\n  description: "x"\n\n'
               'command c:\n  description: "c"\n  do:\n'
               '    reply "in {channel}"\n')
        text2 = emit_cs_bot_text(src, "<test>.newr", "b")
        self.assertIn("_ChannelText(interaction.Channel)", text2)
        self.assertIn("private static string _ChannelText(", text2)

    def test_option_types_and_defaults(self):
        text = emit_shop_text()
        self.assertIn('ApplicationCommandOptionType.String', text)
        self.assertIn('ApplicationCommandOptionType.Integer', text)
        self.assertIn('_Opt(interaction, "item")', text)
        self.assertIn('_Opt(interaction, "amount")', text)
        # Ints stay long in C# (no BigInt conversion).
        self.assertNotIn("BigInt", text)

    def test_option_choices_and_range(self):
        text = emit_shop_text()
        self.assertIn('.AddChoice("brick", "brick")', text)
        self.assertIn('.AddChoice("potion", "potion")', text)
        self.assertIn("WithMinValue(1)", text)
        self.assertIn("WithMaxValue(99)", text)

    def test_top_level_declarations_reach_handlers(self):
        src = ('bot "B":\n  description: "x"\n\n'
               'fn double_it(n: int) -> int:\n  return n * 2\n\n'
               'command c:\n  description: "c"\n  do:\n'
               '    reply "{double_it(21)}"\n')
        text = emit_cs_bot_text(src, "<test>.newr", "b")
        self.assertIn("double_it(21L)", text)

    def test_on_message_ignores_own_messages(self):
        src = ('bot "B":\n  description: "x"\n\n'
               'event message:\n  do:\n'
               '    send "hi" to message.channel\n')
        text = emit_cs_bot_text(src, "<test>.newr", "b")
        self.assertIn("message.Author.Id == bot.CurrentUser.Id", text)
        self.assertIn("MessageReceived", text)


@unittest.skipUnless(DOTNET, "dotnet is not installed")
class TestBotCompiles(unittest.TestCase):
    def test_hello_bot_builds(self):
        result = emit_cs_src(HELLO_BOT, "hello_bot.newr", "hello_bot")
        d = assert_builds(self, result.files, "hello_bot")
        shutil.rmtree(d, ignore_errors=True)

    def test_shop_builds(self):
        result = _emit_shop_result()
        d = assert_builds(self, result.files, "shop")
        shutil.rmtree(d, ignore_errors=True)

    def test_deploy_builds_in_scratch_project(self):
        # The deploy file is a standalone program: it compiles
        # together with the bot file + runtime in a scratch project
        # whose entry point is the deploy class.
        result = emit_cs_src(HELLO_BOT, "hello_bot.newr", "hello_bot")
        files = {
            "hello_bot.cs": result.files["hello_bot.cs"],
            "hello_bot_deploy.cs": result.files["hello_bot_deploy.cs"],
            "newr_rt.cs": RT_CS_SRC.read_text(encoding="utf-8"),
        }
        d = assert_builds(self, files, "hello_bot",
                          startup="hello_botDeploy")
        shutil.rmtree(d, ignore_errors=True)

    def test_reply_select_modal_variants_build(self):
        src = ('bot "B":\n  description: "x"\n\n'
               'command c:\n  description: "c"\n  do:\n'
               '    defer\n'
               '    reply "first"\n'
               '    edit "second"\n'
               '    followup "third"\n'
               '    reply embed:\n'
               '      title: "T"\n'
               '      description: "D"\n'
               '    select "Pick one" (options: ["a", "b"], timeout: 60s):\n'
               '      do:\n'
               '        let n: int = len(values)\n'
               '        reply "picked {n}"\n'
               'command form:\n  description: "f"\n  do:\n'
               '    modal "Feedback" (id: "feedback", timeout: 120s):\n'
               '      text_input "name" (style: short, required):\n'
               '      text_input "notes" (style: paragraph, min_length: 5, max_length: 200):\n'
               '      do:\n'
               '        reply "thanks {name}: {notes}"\n'
               'event message:\n  do:\n'
               '    send "hi {message.channel} {user.id}" to message.channel\n')
        result = emit_cs_src(src, "<test>.newr", "b")
        d = assert_builds(self, result.files, "b")
        shutil.rmtree(d, ignore_errors=True)

    def test_store_harness_end_to_end(self):
        """Drive the emitted shop Bot's store methods against a real
        SQLite file: credit idempotency, debit replay, insufficient
        funds, transfer + replay, get/set of a row."""
        result = _emit_shop_result()
        harness = """
using System;
public class Harness {
  public static void Main(string[] args) {
    shop.Bot bot = new shop.Bot();
    bot.Setup();
    bot.economy__credit("u1", 100L, "k1");
    bot.economy__credit("u1", 100L, "k1");
    Console.WriteLine("balance=" + bot.economy__balance("u1"));
    bool ok = bot.economy__debit("u1", 30L, "k2");
    Console.WriteLine("debit=" + ok + " balance=" + bot.economy__balance("u1"));
    bool ok2 = bot.economy__debit("u1", 30L, "k2");
    Console.WriteLine("debit-replay=" + ok2 + " balance=" + bot.economy__balance("u1"));
    bool poor = bot.economy__debit("u1", 1000L, "k3");
    Console.WriteLine("poor=" + poor + " balance=" + bot.economy__balance("u1"));
    bool tr = bot.economy__transfer("u1", "u2", 20L, "k4");
    Console.WriteLine("transfer=" + tr + " u1=" + bot.economy__balance("u1") + " u2=" + bot.economy__balance("u2"));
    bool tr2 = bot.economy__transfer("u1", "u2", 20L, "k4");
    Console.WriteLine("transfer-replay=" + tr2 + " u1=" + bot.economy__balance("u1") + " u2=" + bot.economy__balance("u2"));
    bot.economy__set("balances", new shop.balances("u3", 5L));
    Console.WriteLine("u3=" + bot.economy__balance("u3"));
    object row = bot.economy__get("balances", "u1");
    Console.WriteLine("get=" + (row == null ? "null" : row.ToString()));
    Console.WriteLine("HARNESS-PASS");
  }
}
"""
        files = dict(result.files)
        files.pop("shop.csproj", None)
        files.pop("shop_deploy.cs", None)
        files["Harness.cs"] = harness
        files["newr_rt.cs"] = RT_CS_SRC.read_text(encoding="utf-8")
        code, out, err, d = build_files(
            files, _bot_csproj("shop", startup="Harness"),
            startup="Harness")
        try:
            self.assertEqual(code, 0, f"dotnet build failed\n{out}\n{err}")
            proc = subprocess.run(
                [DOTNET, "run", "--project", str(d / "build.csproj"),
                 "--no-build"],
                capture_output=True, text=True, cwd=d,
                env=_dotnet_env())
            self.assertEqual(proc.returncode, 0,
                             f"harness exit {proc.returncode}\n"
                             f"{proc.stdout}\n{proc.stderr}")
            self.assertIn("balance=100", proc.stdout)
            self.assertIn("debit=True balance=70", proc.stdout)
            self.assertIn("debit-replay=True balance=70", proc.stdout)
            self.assertIn("poor=False balance=70", proc.stdout)
            self.assertIn("transfer=True u1=50 u2=20", proc.stdout)
            self.assertIn("transfer-replay=True u1=50 u2=20", proc.stdout)
            self.assertIn("u3=5", proc.stdout)
            self.assertIn("balance: 50", proc.stdout)
            self.assertIn("HARNESS-PASS", proc.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
