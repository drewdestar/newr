"""Core stdlib module signatures (SPEC.md §12).

The core builtins and core modules (``math``, ``time``, ``random``,
``file``, ``env``) are part of the language, so this module is imported by
the core checker. It contains NO Discord data - the discord module's
signatures live in ``discord_check.py`` / ``discord_limits.py``.

Signatures are data: ``(parameter types, return type)``. A parameter
type of ``"T"`` means "the element type of the first argument" (for
``random.choice`` / ``min`` / ``max``); the checker resolves it.
``"..."`` in a parameter list means variadic (``print``).

The table is an APPROXIMATE summary, not the final authority: the
checker special-cases some builtins (``_call_builtin``: ``print``,
``ok``/``err``, ``set``, ``float``/``range``) and some signatures
carry checker-only keys (``"optional"``, ``"overload_return"``,
``"variadic"``). Where the table and the checker disagree, the
checker's behaviour wins - and the disagreement is a bug to fix, not
a feature to document.
"""

from __future__ import annotations

from .ast_nodes import BOOL, FLOAT, INT, STR, Type, list_of

# ---------------------------------------------------------------- builtins
# name -> (params, return type, min_positional, variadic)
# ``params`` may contain "T" (element type) and "any-num" (int or float).
BUILTINS: dict[str, dict] = {
    "print": {"params": ["..."], "return": Type("none"), "variadic": True},
    # N-10: `input()` takes 0-1 args (SPEC.md SS12.1: `input(prompt:
    # str = "")`) - the prompt is optional, so params is [] and the
    # prompt is the one optional argument.
    "input": {"params": [], "return": STR, "optional": [STR]},
    "len": {"params": ["lenable"], "return": INT},
    "str": {"params": ["any"], "return": STR},
    "int": {"params": ["any"], "return": INT},
    "float": {"params": ["any"], "return": FLOAT},
    "bool": {"params": ["any"], "return": BOOL},
    "min": {"params": ["T", "T"], "return": "T"},
    "max": {"params": ["T", "T"], "return": "T"},
    "abs": {"params": ["any-num"], "return": "any-num"},
    "round": {"params": ["any-num"], "return": INT,
              "optional": [INT], "overload_return": FLOAT},
    "range": {"params": [INT], "return": list_of(INT),
              "optional": [INT, INT]},
    "set": {"params": [list_of("T")], "return": "set[T]"},
    # Result builders (SPEC.md §10). "T" = the type of the first argument;
    # the checker resolves it (for err, from the expected type).
    "ok": {"params": ["T"], "return": "Result[T, error]"},
    "err": {"params": ["error"], "return": "Result[T, error]"},
}

# Names a user program may not redefine (SPEC.md §9.5 / BUILD_REPORT.md):
# the builtins and the core module names. ``log`` is here because
# ``log.info/warn/error(msg)`` (§12.1) must resolve as a builtin name;
# bare ``log <expr>`` is a statement (a Log node) and never reaches the
# resolver as a name.
RESERVED_BUILTIN_NAMES = set(BUILTINS) | {
    "math", "time", "random", "file", "env", "t", "discord", "log",
}

# ---------------------------------------------------------------- core modules
# module -> member -> signature. "T" = element type of the first argument.
# Constants are {"const": <type>}.
MODULES: dict[str, dict[str, dict]] = {
    "math": {
        "sqrt": {"params": [FLOAT], "return": FLOAT},
        "floor": {"params": [FLOAT], "return": INT},
        "ceil": {"params": [FLOAT], "return": INT},
        "pi": {"const": FLOAT},
        "e": {"const": FLOAT},
    },
    "time": {
        "now": {"params": [], "return": INT},
        "format": {"params": [INT, STR], "return": STR},
    },
    "random": {
        "int": {"params": [INT, INT], "return": INT},
        "choice": {"params": [list_of("T")], "return": "T"},
        "shuffle": {"params": [list_of("T")], "return": Type("none")},
    },
    "file": {
        "read": {"params": [STR], "return": STR},
        "write": {"params": [STR, STR], "return": Type("none")},
        "append": {"params": [STR, STR], "return": Type("none")},
        "exists": {"params": [STR], "return": BOOL},
    },
    "env": {
        # get(name) -> str? (exactly one absent value, spelled T? - §7.2).
        "get": {"params": [STR], "return": Type("str", nullable=True),
                "optional": [STR], "overload_return": STR},
        # args is a value, not a function: the command-line arguments
        # INCLUDING the program name at index 0 (SPEC.md §12.2, G6).
        "args": {"const": list_of(STR)},
    },
}

# time.format's fixed format set (SPEC.md §12.2) - not strftime.
TIME_FORMATS = {"%Y-%m-%d", "%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y", "%H:%M:%S"}

# ---------------------------------------------------------------- methods on values
# type name -> method -> signature. "Self" = the receiver's type,
# "T" = the receiver's element type, "K"/"V" for dicts.
METHODS: dict[str, dict[str, dict]] = {
    "str": {
        "upper": {"params": [], "return": STR},
        "lower": {"params": [], "return": STR},
        "strip": {"params": [], "return": STR},
        "split": {"params": [STR], "return": list_of(STR)},
        "join": {"params": [list_of(STR)], "return": STR},
        "contains": {"params": [STR], "return": BOOL},
        "starts_with": {"params": [STR], "return": BOOL},
        "ends_with": {"params": [STR], "return": BOOL},
        # One Unicode code point per element (SPEC.md §7.6, G8).
        "chars": {"params": [], "return": list_of(STR)},
    },
    "list": {
        "append": {"params": ["T"], "return": Type("none")},
        "contains": {"params": ["T"], "return": BOOL},
        "get": {"params": [INT, "T"], "return": "T"},
        # N-10: SPEC.md SS12.4 lists list.len(); the checker rejected
        # it because the method was missing from this table.
        "len": {"params": [], "return": INT},
    },
    "dict": {
        "keys": {"params": [], "return": list_of(STR)},
        "get": {"params": [STR, "V"], "return": "V"},
        "contains": {"params": [STR], "return": BOOL},
    },
    "set": {
        "add": {"params": ["T"], "return": Type("none")},
        "remove": {"params": ["T"], "return": Type("none")},
        "contains": {"params": ["T"], "return": BOOL},
    },
    # Methods on T? - keyed "nullable" (the receiver's inner type is "T").
    "nullable": {
        "is_some": {"params": [], "return": BOOL},
        "is_none": {"params": [], "return": BOOL},
        "unwrap": {"params": [], "return": "T"},
    },
    # Methods on Result[T, E] (SPEC.md §10).
    "Result": {
        "is_ok": {"params": [], "return": BOOL},
        "is_err": {"params": [], "return": BOOL},
        "unwrap": {"params": [], "return": "T"},
        "unwrap_or": {"params": ["T"], "return": "T"},
    },
}
