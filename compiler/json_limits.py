"""Limits and constants for the `json` module (SPEC.md section 12.7,
M8b).

This module holds ONLY data: the surface names, the encodable scalar
types, and the float rule. It holds no rules and no diagnostics --
those live in ``json_check.py``. The core checker never imports this
module (SPEC.md SS2.1, the json twin of the db rule); only
``json_check.py`` (and the CLI, which composes the checkers, and
``docs.py``, which renders the surface) does.
"""

from __future__ import annotations

#: The error code `json_check.py` reports (SPEC.md section 16).
JSON_ERROR_CODE = "E2013"

#: The complete `json` surface (SPEC.md section 12.7). `encode`
#: returns a str; `decode` returns the expected type at the call
#: site.
JSON_FUNCTIONS = (
    "encode", "decode",
)

#: Scalar types a JSON value may hold (SPEC.md section 12.7). A
#: struct, a list, a dict, or a nullable of any encodable type is
#: encodable too, recursively. An enum is NOT encodable in M8b.
JSON_SCALAR_TYPES = {"int", "float", "str", "bool"}

#: The float rendering rule, pinned (SPEC.md section 12.7): a float
#: encodes as its shortest round-trip decimal, with ".0" appended
#: when the value is integral. Byte-identical encoded output across
#: targets is guaranteed only inside this range; outside it the
#: representations diverge (exponent forms) while decoding stays
#: identical.
JSON_FLOAT_IDENTICAL_MIN = 0.001
JSON_FLOAT_IDENTICAL_MAX = 1e15

#: The `json` surface as data (SPEC.md section 12.7). `json_check.py`
#: validates calls against this table; `docs.py` renders it into
#: `docs/api.md`, so the generated reference and the checker can
#: never drift apart.
JSON_MEMBERS: dict[str, dict[str, str]] = {
    "encode": {
        "sig": "encode(value) -> str",
        "doc": "Encodes a value as JSON text. The value may be a "
               "struct, a list, a dict, a str, an int, a float, a "
               "bool, or a nullable of any of those (none encodes as "
               "`null`), nested recursively. Structs encode as "
               "objects, field names as keys, in declaration order; "
               "nullable fields that are none encode as `null` (they "
               "are not omitted). Enums are NOT encodable in M8b. "
               "Floats encode as their shortest round-trip decimal, "
               "with \".0\" for integral floats (1.0 encodes as "
               "\"1.0\"). Ints encode as exact decimal, including "
               "int64.",
    },
    "decode": {
        "sig": "decode(text: str) -> T",
        "doc": "Decodes JSON text into the expected type at the call "
               "site, for example `let todos: list[Todo] = "
               "json.decode(text)`. The expected type may be a "
               "struct, a list, a dict, a str, an int, a float, a "
               "bool, or a nullable of any of those. A JSON `null` "
               "decodes to `none` only for a nullable expected type. "
               "Struct fields match by name; a missing field throws, "
               "an extra field is ignored. A JSON number with a "
               "fraction into an `int` field throws; a JSON integer "
               "into a `float` field is fine. Trailing data after "
               "the JSON value throws, and an empty string throws. "
               "Duplicate keys: last wins.",
    },
}
