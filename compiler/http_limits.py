"""Limits and constants for the `http` module (SPEC.md section 12.6,
M8b).

This module holds ONLY data: the surface names, the HTTP methods, the
timeout rule, and the allowlist shape. It holds no rules and no
diagnostics -- those live in ``http_check.py``. The core checker never
imports this module (SPEC.md SS2.1, the http twin of the db rule); only
``http_check.py`` (and the CLI, which composes the checkers, and
``docs.py``, which renders the surface) does.
"""

from __future__ import annotations

#: The error code `http_check.py` reports (SPEC.md section 16).
HTTP_ERROR_CODE = "E2012"

#: The complete `http` surface (SPEC.md section 12.6). `get`, `post`,
#: and `request` return a typed HttpResponse struct, resolved from the
#: expected type at the call site; `route` and `serve` return none.
HTTP_FUNCTIONS = (
    "route", "serve", "get", "post", "request",
)

#: Methods a literal may name (SPEC.md section 12.6). Uppercase only;
#: a lowercase or unknown method literal is E2012. `get` and `post`
#: are conveniences; every other method goes through `request`.
HTTP_METHODS = {
    "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
}

#: Default fetch timeout, whole seconds (SPEC.md section 12.6). A
#: timeout of 0 or negative is E2012.
DEFAULT_HTTP_TIMEOUT_SECONDS = 10

#: The host `http.serve` binds when no host argument is given
#: (SPEC.md section 12.6). Only an explicit host argument listens
#: on all interfaces ("0.0.0.0").
HTTP_DEFAULT_HOST = "127.0.0.1"

#: The allowlist when no manifest exists, or when the manifest sets
#: no `http_allowlist` key (SPEC.md section 12.6): localhost only.
DEFAULT_HTTP_ALLOWLIST = ("localhost", "127.0.0.1")

#: The `HttpRequest` field shape, as data (SPEC.md section 12.6):
#: (field name, type spec) in declaration order. A type spec is a
#: bare name ("str") or a tuple (container, element, ...) for
#: `dict[str, str]`. `http_check.py` validates a handler parameter
#: struct (and a fetch response struct) against these, by field
#: shape, not by struct name. All five fields are required, with
#: exactly these types; extra fields are an error.
HTTP_REQUEST_FIELDS: tuple[tuple[str, object], ...] = (
    ("method", "str"),
    ("path", "str"),
    ("headers", ("dict", "str", "str")),
    ("body", "str"),
    ("query", ("dict", "str", "str")),
)

#: The `HttpResponse` field shape, as data (SPEC.md section 12.6):
#: all three fields required, exactly these types, no extras.
HTTP_RESPONSE_FIELDS: tuple[tuple[str, object], ...] = (
    ("status", "int"),
    ("headers", ("dict", "str", "str")),
    ("body", "str"),
)

#: The `http` surface as data (SPEC.md section 12.6). `http_check.py`
#: validates calls against this table; `docs.py` renders it into
#: `docs/api.md`, so the generated reference and the checker can
#: never drift apart. Fetch returns a typed HttpResponse struct,
#: resolved from the expected type at the call site; `route` and
#: `serve` return none.
HTTP_MEMBERS: dict[str, dict[str, str]] = {
    "route": {
        "sig": "route(method: str, path: str, handler: fn) -> none",
        "doc": "Registers one exact-match route. The method must be "
               "one of GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS "
               "(uppercase); the path must start with \"/\". The "
               "handler is a lambda taking one request struct (method, "
               "path, headers, body, query) and returning a response "
               "struct (status, headers, body). Two routes with the "
               "same method and path are an error. A request with no "
               "route returns 404 with an empty body.",
    },
    "serve": {
        "sig": "serve(port: int) -> none; serve(port: int, host: str) -> none",
        "doc": "Serves the registered routes forever, until the "
               "process is killed. Binds 127.0.0.1 by default; an "
               "explicit host binds that host instead (\"0.0.0.0\" "
               "listens on all interfaces). Port 0 means the OS picks "
               "an ephemeral port, and the server prints `listening "
               "on <host>:<port>` with the real port as its first "
               "line. Put `serve` last: it never returns.",
    },
    "get": {
        "sig": "get(url: str) -> HttpResponse; "
               "get(url: str, timeout: int) -> HttpResponse; "
               "get(url: str, timeout: int, headers: dict[str, str]) -> HttpResponse",
        "doc": "Fetches a URL with GET and returns the response as a "
               "typed struct (status, headers, body), resolved from "
               "the expected type at the call site, for example `let "
               "resp: HttpResponse = http.get(url)`. The timeout is "
               "whole seconds (default 10) and covers the whole "
               "request. The URL host must be on the allowlist "
               "(newr.json `http_allowlist`, default localhost only). "
               "Throws on failure; use try/catch to handle it.",
    },
    "post": {
        "sig": "post(url: str, body: str) -> HttpResponse; "
               "post(url: str, body: str, timeout: int) -> HttpResponse; "
               "post(url: str, body: str, timeout: int, headers: dict[str, str]) -> HttpResponse",
        "doc": "Fetches a URL with POST, sending the body as given, "
               "and returns the response as a typed struct (status, "
               "headers, body), resolved from the expected type at "
               "the call site. Sets `Content-Type: application/json` "
               "unless the program sets one. The timeout is whole "
               "seconds (default 10). The URL host must be on the "
               "allowlist. Throws on failure.",
    },
    "request": {
        "sig": "request(method: str, url: str, body: str) -> HttpResponse; "
               "request(method: str, url: str, body: str, timeout: int) -> HttpResponse; "
               "request(method: str, url: str, body: str, timeout: int, headers: dict[str, str]) -> HttpResponse",
        "doc": "The generic fetch primitive: any method (GET, POST, "
               "PUT, PATCH, DELETE, HEAD, OPTIONS), any URL, a body "
               "sent as given. Returns the response as a typed "
               "struct (status, headers, body), resolved from the "
               "expected type at the call site. Sets `Content-Type: "
               "application/json` when a body is sent and the program "
               "sets none. The timeout is whole seconds (default 10). "
               "The URL host must be on the allowlist. Throws on "
               "failure.",
    },
}
