"""Limits and constants for the `db` module (SPEC.md section 12.5,
M8a).

This module holds ONLY data: the surface names, the bindable scalar
types, and the SQLite pragmas the emitters inherit from the `store`
implementation. It holds no rules and no diagnostics -- those live in
``db_check.py``. The core checker never imports this module (SPEC.md
SS2.1, the db twin of the discord rule); only ``db_check.py`` (and the
CLI, which composes the checkers) does.
"""

from __future__ import annotations

#: The error code `db_check.py` reports (SPEC.md section 16).
DB_ERROR_CODE = "E2011"

#: The complete `db` surface (SPEC.md section 12.5). `query` returns a
#: typed struct list; every other function has a fixed return type.
DB_FUNCTIONS = (
    "open", "exec", "query", "begin", "commit", "rollback", "last_id",
    "close",
)

#: Types a `?` placeholder may bind (SPEC.md section 12.5). A list, a
#: dict, a struct, `none`, or any other value is not bindable -- the
#: call is E2011. A NULL column value comes from omitting the column
#: (DEFAULT NULL) or from a literal NULL in the SQL text, not from a
#: bound argument. Nullable (`T?`) values are not bindable either:
#: unwrap them first.
DB_BINDABLE_TYPES = {"int", "float", "str", "bool"}

#: Types a row struct field may have (SPEC.md section 12.5). A field
#: of any other type (a list, a dict, another struct) cannot be read
#: back from a row -- the query is E2011. Nullable (`T?`) fields are
#: allowed: NULL maps to `none`.
DB_ROW_FIELD_TYPES = {"int", "float", "str", "bool"}

#: Busy timeout on every connection, inherited from `store` (SPEC.md
#: section 14): a locked database waits 5 seconds before failing.
SQLITE_BUSY_TIMEOUT_MS = 5000

#: Foreign keys are enforced on every connection, inherited from
#: `store`.
SQLITE_FOREIGN_KEYS_ON = True

#: The `db` surface as data (SPEC.md section 12.5). `db_check.py`
#: validates calls against this table; `docs.py` renders it into
#: `docs/api.md`, so the generated reference and the checker can
#: never drift apart. `query` returns a typed struct list, resolved
#: from the expected type at the call site; every other function has
#: a fixed return type.
DB_MEMBERS: dict[str, dict[str, str]] = {
    "open": {
        "sig": "open(path: str) -> int",
        "doc": "Opens a SQLite database file and returns a connection "
               "handle (an int, fd style). Creates the file when "
               "missing; throws when the parent directory does not "
               "exist. `\":memory:\"` opens a private in-memory "
               "database.",
    },
    "exec": {
        "sig": "exec(handle: int, sql: str, args...) -> int",
        "doc": "Runs exactly one statement and returns rows affected "
               "(0 for DDL). Values bind to `?` placeholders, one "
               "argument per placeholder, in order. Throws on "
               "failure.",
    },
    "query": {
        "sig": "query(handle: int, sql: str, args...) -> list[RowStruct]",
        "doc": "Runs a SELECT and returns the rows as a typed struct "
               "list. The call site must carry the expected type, "
               "for example `let todos: list[Todo] = db.query(...)`. "
               "Columns match struct fields by name; a missing column "
               "or a value that cannot convert throws at runtime.",
    },
    "begin": {
        "sig": "begin(handle: int)",
        "doc": "Begins an explicit transaction (BEGIN IMMEDIATE). A "
               "second begin without commit or rollback is an error.",
    },
    "commit": {
        "sig": "commit(handle: int)",
        "doc": "Commits the open transaction.",
    },
    "rollback": {
        "sig": "rollback(handle: int)",
        "doc": "Rolls the open transaction back.",
    },
    "last_id": {
        "sig": "last_id(handle: int) -> int",
        "doc": "The last AUTOINCREMENT id on this handle.",
    },
    "close": {
        "sig": "close(handle: int)",
        "doc": "Closes the handle. Using a closed handle throws.",
    },
}
