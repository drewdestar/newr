"""Discord-owned constants and rules (SPEC.md §2.1, §13).

This module is owned by the ``discord`` domain module. ONLY
``discord_check.py`` (and the CLI, to build the type provider) may import
it. The core checker (``checker.py``) MUST NEVER import this module — the
stability contract (§2.1) says the core language has zero Discord
dependencies. ``tests/test_boundaries.py`` enforces the boundary.

If Discord changes a limit, an API, or a rule, the change lands HERE (and
in ``discord_check.py``), never in the core language. A limit change is a
module change, not a language change (§2.2).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------- command names
# Discord's rule (SPEC.md §13.2.1), ported verbatim from the v0 compiler
# (which mirrors discord.py's stdlib translation of Discord's own rule,
# including Thai/Devanagari combining marks and U+02BC).
_THAI_COMBINING = r"\u0e31-\u0e3a\u0e47-\u0e4e"
_DEVANAGARI_COMBINING = (
    r"\u0900-\u0903\u093a\u093b\u093c\u093e\u093f\u0940-\u094f"
    r"\u0955\u0956\u0957\u0962\u0963")
COMMAND_NAME_RE = re.compile(
    r"^[-_\u02bc\w" + _THAI_COMBINING + _DEVANAGARI_COMBINING + r"]{1,32}$")

COMMAND_NAME_MIN = 1
COMMAND_NAME_MAX = 32

# ---------------------------------------------------------------- descriptions / replies
COMMAND_DESCRIPTION_MAX = 100
REPLY_MAX = 2000
OPTION_DESCRIPTION_MAX = 100
OPTION_NAME_MAX = 32

# Placeholders expand at runtime: a mention is up to 22 characters
# (<@123456789012345678>) and a server name up to 100. Measure a reply at
# its longest possible expansion, so a build that passes never fails at
# send time against Discord's 2000-character limit. (v0 rule, kept.)
# This table covers the v0 placeholders; ENTITY_FIELD_MAX below covers
# the general interpolated field accesses (SPEC.md §13.4).
PLACEHOLDER_MAX = {"user": 22, "guild": 100, "channel": 22, "bot": 22}

# Max expanded length of an interpolated entity-field access, in
# characters. A snowflake id is at most 19 digits; a Discord username is
# at most 32 characters; a guild/channel/role name is at most 100.
ENTITY_FIELD_MAX = {
    "user.mention": 22,
    "user.name": 32,
    "user.id": 19,
    "user.bot": 5,  # "True" / "False"
    "member.mention": 22,
    "member.name": 32,
    "member.id": 19,
    "guild.name": 100,
    "guild.id": 19,
    "guild.member_count": 10,
    "channel.mention": 22,
    "channel.name": 100,
    "channel.id": 19,
    "role.name": 100,
    "role.id": 19,
    "bot.mention": 22,
    "bot.name": 32,
    "bot.id": 19,
}

# Max expanded length of a scalar value in interpolation.
INT_MAX_LEN = 19      # a 64-bit signed integer, with sign
FLOAT_MAX_LEN = 24    # a double printed in full
BOOL_MAX_LEN = 5      # "True" / "False"

# ---------------------------------------------------------------- options
OPTIONS_PER_COMMAND_MAX = 25
OPTION_CHOICES_MAX = 25

# ---------------------------------------------------------------- components
CUSTOM_ID_MAX = 100
ACTION_ROWS_PER_MESSAGE_MAX = 5
COMPONENTS_PER_MODAL_MAX = 5
SELECT_OPTIONS_MAX = 25

BUTTON_STYLES = {"primary", "secondary", "success", "danger", "link"}
TEXT_INPUT_STYLES = {"short", "paragraph"}

# ---------------------------------------------------------------- embeds
EMBED_TITLE_MAX = 256
EMBED_DESCRIPTION_MAX = 4096
EMBED_FIELDS_MAX = 25
EMBED_FIELD_NAME_MAX = 256
EMBED_FIELD_VALUE_MAX = 1024
EMBED_TOTAL_MAX = 6000
EMBED_COLOR_MIN = 0
EMBED_COLOR_MAX = 0xFFFFFF

# ---------------------------------------------------------------- bot / events
BOT_NAME_MIN = 1
BOT_NAME_MAX = 32

EVENT_KINDS = {"ready", "member_join", "member_remove", "message"}

# Placeholder contexts (v0 rule, kept — SPEC.md §13.4). The general
# language enforces this by scoping (the resolver only injects these
# bindings in their contexts); this table documents the v0 contract.
PLACEHOLDER_CONTEXTS = {
    "command": {"user", "guild", "channel"},
    "ready": {"bot"},
    "member_join": {"user", "guild"},
    "member_remove": {"user", "guild"},
}

# ---------------------------------------------------------------- intents
# Privileged intents (Discord requires verification/approval for bots in
# 10,000+ servers — the threshold is a Discord-platform rule, not a
# library rule). Declaring one is a warning (§13.6).
PRIVILEGED_INTENTS = {"members", "message_content", "presences"}

# Which constructs require which intents (derived + validated, §13.6).
INTENT_REQUIRED = {
    "member_join": {"members"},
    "member_remove": {"members"},
    "message": {"message_content"},
}

KNOWN_INTENTS = {
    "guilds", "members", "moderation", "emojis", "integrations",
    "webhooks", "invites", "voice_states", "presences",
    "guild_messages", "direct_messages", "guild_reactions",
    "direct_message_reactions", "guild_typing", "direct_message_typing",
    "message_content", "guild_scheduled_events", "auto_moderation",
    "guild_polls", "direct_message_polls",
}

# ---------------------------------------------------------------- entity fields
# Exposed entity fields (SPEC.md §7.3). Anything else is a build error.
# The checker receives this table as a type provider (dependency
# injection) so the core never imports this module (§2.1).
# Values are the newr type names of the fields.
ENTITY_FIELDS: dict[str, dict[str, str]] = {
    "user": {
        "id": "int",
        "mention": "str",
        "name": "str",
        "bot": "bool",
    },
    "member": {
        "id": "int",
        "mention": "str",
        "name": "str",
        "guild": "guild",
    },
    "guild": {
        "id": "int",
        "name": "str",
        "member_count": "int",
    },
    "channel": {
        "id": "int",
        "name": "str",
        "mention": "str",
    },
    "role": {
        "id": "int",
        "name": "str",
    },
    "interaction": {
        "user": "user",
        "guild": "guild",
        "channel": "channel",
        "locale": "str",
    },
    "message": {
        "content": "str",
        "author": "user",
        "channel": "channel",
    },
}


def fn_name(command_name: str) -> str:
    """A valid function name for a command, e.g. ``cmd_halo_dunia``.

    ``-`` is legal in a Discord command name and illegal in an identifier,
    so it becomes ``_``. Two different command names can sanitize to the
    same function name (``foo-bar`` and ``foo_bar``); the checker rejects
    that pair (v0 rule, kept).
    """
    return "cmd_" + command_name.replace("-", "_")
