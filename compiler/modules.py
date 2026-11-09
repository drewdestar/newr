"""Core stdlib module signatures (SPEC.md §12).

The core builtins and core modules (``math``, ``time``, ``random``,
``file``, ``env``) are part of the language, so this module is imported by
the core checker. It contains NO Discord data — the discord module's
signatures live in ``discord_check.py`` / ``discord_limits.py`` — and
NO db data — the db module's signatures live in ``db_check.py`` /
``db_limits.py``.

Signatures are data: ``(parameter types, return type)``. A parameter
type of ``"T"`` means "the element type of the first argument" (for
``random.choice`` / ``min`` / ``max``); the checker resolves it.
``"..."`` in a parameter list means variadic (``print``).

The table is an APPROXIMATE summary, not the final authority: the
checker special-cases some builtins (``_call_builtin``: ``print``,
``ok``/``err``, ``set``, ``float``/``range``) and some signatures
carry checker-only keys (``"optional"``, ``"overload_return"``,
``"variadic"``). Where the table and the checker disagree, the
checker's behaviour wins — and the disagreement is a bug to fix, not
a feature to document.
"""

from __future__ import annotations

from .ast_nodes import BOOL, FLOAT, INT, STR, Type, list_of

# ---------------------------------------------------------------- builtins
# name -> (params, return type, min_positional, variadic)
# ``params`` may contain "T" (element type) and "any-num" (int or float).
BUILTINS: dict[str, dict] = {
    "print": {"params": ["..."], "return": Type("none"), "variadic": True,
              "sig": "print(expr, ...)",
              "doc": "Writes the values space-separated plus a newline "
                     "to stdout."},
    # N-10: `input()` takes 0-1 args (SPEC.md SS12.1: `input(prompt:
    # str = "")`) — the prompt is optional, so params is [] and the
    # prompt is the one optional argument.
    "input": {"params": [], "return": STR, "optional": [STR],
              "sig": 'input(prompt: str = "") -> str',
              "doc": "Reads one line from stdin (blocking). The prompt "
                     "is optional."},
    "len": {"params": ["lenable"], "return": INT,
            "sig": "len(x) -> int",
            "doc": "Length of a str (characters), list, dict, or set."},
    "str": {"params": ["any"], "return": STR,
            "sig": "str(x) -> str",
            "doc": "Converts int, float, or bool to its text form."},
    "int": {"params": ["any"], "return": INT,
            "sig": "int(x) -> int",
            "doc": "From str (parses, throws on invalid) or float "
                   "(truncates)."},
    "float": {"params": ["any"], "return": FLOAT,
              "sig": "float(x) -> float",
              "doc": "From str or int."},
    "bool": {"params": ["any"], "return": BOOL,
             "sig": "bool(x) -> bool",
             "doc": "From str (\"true\"/\"false\") or int (0 is false)."},
    "min": {"params": ["T", "T"], "return": "T",
            "sig": "min(a, b) -> T",
            "doc": "The smaller of two same-type numbers (or strs, "
                   "lexicographic)."},
    "max": {"params": ["T", "T"], "return": "T",
            "sig": "max(a, b) -> T",
            "doc": "The larger of two same-type numbers (or strs, "
                   "lexicographic)."},
    "abs": {"params": ["any-num"], "return": "any-num",
            "sig": "abs(x) -> T",
            "doc": "Absolute value of a number."},
    "round": {"params": ["any-num"], "return": INT,
              "optional": [INT], "overload_return": FLOAT,
              "sig": "round(x) -> int / round(x, n: int) -> float",
              "doc": "Rounds to an int, or to n places as a float."},
    "range": {"params": [INT], "return": list_of(INT),
              "optional": [INT, INT],
              "sig": "range(n) -> list[int] / range(a, b) -> list[int] "
                     "/ range(a, b, step) -> list[int]",
              "doc": "Half-open list [a, b) of ints."},
    "set": {"params": [list_of("T")], "return": "set[T]",
            "sig": "set(list[T]) -> set[T]",
            "doc": "Builds a set from a list. There is no set literal "
                   "in v1."},
    # Result builders (SPEC.md §10). "T" = the type of the first argument;
    # the checker resolves it (for err, from the expected type).
    "ok": {"params": ["T"], "return": "Result[T, error]",
           "sig": "ok(v) -> Result[T, error]",
           "doc": "Builds a successful Result value."},
    "err": {"params": ["error"], "return": "Result[T, error]",
            "sig": "err(e) -> Result[T, error]",
            "doc": "Builds a failed Result value."},
}

# Names a user program may not redefine (SPEC.md §9.5 / BUILD_REPORT.md):
# the builtins and the core module names. ``log`` is here because
# ``log.info/warn/error(msg)`` (§12.1) must resolve as a builtin name;
# bare ``log <expr>`` is a statement (a Log node) and never reaches the
# resolver as a name.
RESERVED_BUILTIN_NAMES = set(BUILTINS) | {
    "math", "time", "random", "file", "env", "t", "discord", "db", "log",
}

# ---------------------------------------------------------------- core modules
# module -> member -> signature. "T" = element type of the first argument.
# Constants are {"const": <type>}.
MODULES: dict[str, dict[str, dict]] = {
    "math": {
        "sqrt": {"params": [FLOAT], "return": FLOAT,
                 "sig": "sqrt(x: float) -> float",
                 "doc": "Square root."},
        "floor": {"params": [FLOAT], "return": INT,
                  "sig": "floor(x: float) -> int",
                  "doc": "Rounds down to the nearest int."},
        "ceil": {"params": [FLOAT], "return": INT,
                 "sig": "ceil(x: float) -> int",
                 "doc": "Rounds up to the nearest int."},
        "pi": {"const": FLOAT,
               "sig": "pi: float",
               "doc": "The constant pi."},
        "e": {"const": FLOAT,
              "sig": "e: float",
              "doc": "The constant e."},
    },
    "time": {
        "now": {"params": [], "return": INT,
                "sig": "now() -> int",
                "doc": "Current time as unix seconds."},
        "format": {"params": [INT, STR], "return": STR,
                   "sig": "format(ts: int, fmt: str) -> str",
                   "doc": "Formats a unix timestamp with one of the "
                          "fixed formats (not strftime)."},
    },
    "random": {
        "int": {"params": [INT, INT], "return": INT,
                "sig": "int(a: int, b: int) -> int",
                "doc": "A random int in [a, b]."},
        "choice": {"params": [list_of("T")], "return": "T",
                   "sig": "choice(list[T]) -> T",
                   "doc": "A random element of a list."},
        "shuffle": {"params": [list_of("T")], "return": Type("none"),
                    "sig": "shuffle(list[T])",
                    "doc": "Shuffles a list in place."},
    },
    "file": {
        "read": {"params": [STR], "return": STR,
                 "sig": "read(path: str) -> str",
                 "doc": "Reads a UTF-8 text file. Throws on failure."},
        "write": {"params": [STR, STR], "return": Type("none"),
                  "sig": "write(path: str, content: str)",
                  "doc": "Writes a UTF-8 text file, replacing it. "
                         "Throws on failure."},
        "append": {"params": [STR, STR], "return": Type("none"),
                   "sig": "append(path: str, content: str)",
                   "doc": "Appends to a UTF-8 text file. Throws on "
                          "failure."},
        "exists": {"params": [STR], "return": BOOL,
                   "sig": "exists(path: str) -> bool",
                   "doc": "Whether a file exists."},
    },
    "env": {
        # get(name) -> str? (exactly one absent value, spelled T? — §7.2).
        "get": {"params": [STR], "return": Type("str", nullable=True),
                "optional": [STR], "overload_return": STR,
                "sig": "get(name: str) -> str? / "
                       "get(name: str, default: str) -> str",
                "doc": "Reads an environment variable, none when "
                       "absent (or the default)."},
        # args is a value, not a function: the command-line arguments
        # INCLUDING the program name at index 0 (SPEC.md §12.2, G6).
        "args": {"const": list_of(STR),
                 "sig": "args: list[str]",
                 "doc": "Command-line arguments, including the program "
                        "name at index 0."},
    },
}

# time.format's fixed format set (SPEC.md §12.2) — not strftime.
TIME_FORMATS = {"%Y-%m-%d", "%H:%M", "%Y-%m-%d %H:%M", "%d/%m/%Y", "%H:%M:%S"}

# ---------------------------------------------------------------- methods on values
# type name -> method -> signature. "Self" = the receiver's type,
# "T" = the receiver's element type, "K"/"V" for dicts.
METHODS: dict[str, dict[str, dict]] = {
    "str": {
        "upper": {"params": [], "return": STR,
                  "doc": "Uppercase copy."},
        "lower": {"params": [], "return": STR,
                  "doc": "Lowercase copy."},
        "strip": {"params": [], "return": STR,
                  "doc": "Copy with leading and trailing whitespace "
                         "removed."},
        "split": {"params": [STR], "return": list_of(STR),
                  "doc": "Splits on the separator."},
        "join": {"params": [list_of(STR)], "return": STR,
                 "doc": "Joins a list of strs with this string between "
                        "them."},
        "contains": {"params": [STR], "return": BOOL,
                     "doc": "Whether the string contains the argument."},
        "starts_with": {"params": [STR], "return": BOOL,
                        "doc": "Whether the string starts with the "
                               "argument."},
        "ends_with": {"params": [STR], "return": BOOL,
                      "doc": "Whether the string ends with the "
                             "argument."},
        # One Unicode code point per element (SPEC.md §7.6, G8).
        "chars": {"params": [], "return": list_of(STR),
                  "doc": "One Unicode code point per element."},
    },
    "list": {
        "append": {"params": ["T"], "return": Type("none"),
                   "doc": "Appends an element."},
        "contains": {"params": ["T"], "return": BOOL,
                     "doc": "Whether the list contains the element."},
        "get": {"params": [INT, "T"], "return": "T",
                "doc": "Element at the index, or the default when out "
                       "of range."},
        # N-10: SPEC.md SS12.4 lists list.len(); the checker rejected
        # it because the method was missing from this table.
        "len": {"params": [], "return": INT,
                "doc": "Number of elements."},
    },
    "dict": {
        "keys": {"params": [], "return": list_of(STR),
                 "doc": "Keys in insertion order."},
        "get": {"params": [STR, "V"], "return": "V",
                "doc": "Value for the key, or the default when absent."},
        "contains": {"params": [STR], "return": BOOL,
                     "doc": "Whether the dict has the key."},
    },
    "set": {
        "add": {"params": ["T"], "return": Type("none"),
                "doc": "Adds an element."},
        "remove": {"params": ["T"], "return": Type("none"),
                   "doc": "Removes an element."},
        "contains": {"params": ["T"], "return": BOOL,
                     "doc": "Whether the set contains the element."},
    },
    # Methods on T? — keyed "nullable" (the receiver's inner type is "T").
    "nullable": {
        "is_some": {"params": [], "return": BOOL,
                    "doc": "Whether the value is present."},
        "is_none": {"params": [], "return": BOOL,
                    "doc": "Whether the value is absent (none)."},
        "unwrap": {"params": [], "return": "T",
                   "doc": "The value, or an error when absent."},
    },
    # Methods on Result[T, E] (SPEC.md §10).
    "Result": {
        "is_ok": {"params": [], "return": BOOL,
                  "doc": "Whether the Result is ok."},
        "is_err": {"params": [], "return": BOOL,
                   "doc": "Whether the Result is err."},
        "unwrap": {"params": [], "return": "T",
                   "doc": "The ok value, or an error when err."},
        "unwrap_or": {"params": ["T"], "return": "T",
                      "doc": "The ok value, or the default when err."},
    },
}
