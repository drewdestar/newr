"""Tests for the http checker (SPEC.md SS12.6, M8b).

Written FIRST (TDD RED). The helper runs the full front end:
resolve -> core check (with discord providers) -> discord check
-> db check -> http check -> json check.
"""

import unittest

from compiler import ast_nodes as A
from compiler.checker import check_program
from compiler.db_check import check_db
from compiler.discord_check import check_discord, discord_providers
from compiler.http_check import check_http
from compiler.json_check import check_json
from compiler.resolver import resolve_source


def full(src, allowlist=None):
    """All diagnostics from resolve + core check + discord + db +
    http + json."""
    resolved = resolve_source(src)
    diags = list(resolved.diagnostics)
    diags += check_program(resolved.program,
                           providers=discord_providers(resolved.program)
                           ).diagnostics
    diags += check_discord(resolved.program)
    diags += check_db(resolved.program)
    diags += check_http(resolved.program, allowlist=allowlist)
    diags += check_json(resolved.program)
    return diags


def core_only(src):
    """Diagnostics from resolve + core check only (no domain
    checker): the http rules must live in http_check, never in
    core."""
    resolved = resolve_source(src)
    diags = list(resolved.diagnostics)
    diags += check_program(resolved.program,
                           providers=discord_providers(resolved.program)
                           ).diagnostics
    return diags


def codes(diags):
    return [d.code for d in diags]


def errs(diags):
    return [d for d in diags if d.is_error()]


def messages(diags):
    return [d.message for d in errs(diags)]


def helps(diags):
    return [d.help or "" for d in errs(diags)]


REQ_RESP = """struct HttpRequest:
  method: str
  path: str
  headers: dict[str, str]
  body: str
  query: dict[str, str]

struct HttpResponse:
  status: int
  headers: dict[str, str]
  body: str

"""

TODO_STRUCT = """struct Todo:
  id: int
  title: str
  done: bool

"""

#: A handler bound with a let (the multi-statement shape: a
#: block-body lambda cannot be written inline as a call argument).
HANDLER = ('let list_todos = fn(req: HttpRequest) -> HttpResponse:\n'
           '  return HttpResponse(status: 200, headers: {}, body: "[]")\n')

#: The same handler inline, single-expression form.
INLINE_HANDLER = ('fn(req: HttpRequest) -> HttpResponse: '
                  'HttpResponse(status: 200, headers: {}, body: "[]")')


def main_body(body):
    return REQ_RESP + "fn main():\n" + body


class TestHttpHappyPath(unittest.TestCase):
    def test_full_happy_path(self):
        src = main_body(
            "  " + HANDLER.replace("\n", "\n  ").rstrip() + "\n"
            + '  http.route("GET", "/todos", list_todos)\n'
            + '  http.route("POST", "/todos", ' + INLINE_HANDLER + ')\n'
            + '  http.serve(8080)\n'
            + '  let resp: HttpResponse = http.get('
            '"http://127.0.0.1:8080/todos")\n'
            + '  print(resp.status)\n')
        self.assertEqual(errs(full(src)), [])

    def test_http_resolves_without_import(self):
        # Named modules are usable by name (SPEC.md SS12.2); "http"
        # is in RESERVED_BUILTIN_NAMES, so no E3001.
        src = main_body('  http.serve(8080)\n')
        self.assertEqual(errs(full(src)), [])

    def test_explicit_import(self):
        src = "import http\n\n" + main_body('  http.serve(8080)\n')
        self.assertEqual(errs(full(src)), [])

    def test_aliased_import(self):
        src = "import http as h\n\n" + main_body('  h.serve(8080)\n')
        self.assertEqual(errs(full(src)), [])

    def test_route_with_inline_expr_lambda(self):
        src = main_body(
            '  http.route("GET", "/todos", ' + INLINE_HANDLER + ')\n')
        self.assertEqual(errs(full(src)), [])

    def test_serve_with_host(self):
        src = main_body('  http.serve(0, "0.0.0.0")\n')
        self.assertEqual(errs(full(src)), [])

    def test_serve_port_zero_is_ephemeral(self):
        src = main_body('  http.serve(0)\n')
        self.assertEqual(errs(full(src)), [])

    def test_get_all_arities(self):
        src = main_body(
            '  let a: HttpResponse = http.get('
            '"http://127.0.0.1:8080/todos")\n'
            '  let b: HttpResponse = http.get('
            '"http://127.0.0.1:8080/todos", 5)\n'
            '  let c: HttpResponse = http.get('
            '"http://127.0.0.1:8080/todos", 5, {})\n'
            '  print(a.status + b.status + c.status)\n')
        self.assertEqual(errs(full(src)), [])

    def test_post_all_arities(self):
        src = main_body(
            '  let a: HttpResponse = http.post('
            '"http://127.0.0.1:8080/todos", "[]")\n'
            '  let b: HttpResponse = http.post('
            '"http://127.0.0.1:8080/todos", "[]", 5)\n'
            '  let c: HttpResponse = http.post('
            '"http://127.0.0.1:8080/todos", "[]", 5, {})\n'
            '  print(a.status + b.status + c.status)\n')
        self.assertEqual(errs(full(src)), [])

    def test_request_all_arities(self):
        src = main_body(
            '  let a: HttpResponse = http.request("PUT", '
            '"http://127.0.0.1:8080/todos", "[]")\n'
            '  let b: HttpResponse = http.request("DELETE", '
            '"http://127.0.0.1:8080/todos", "", 5)\n'
            '  let c: HttpResponse = http.request("PATCH", '
            '"http://127.0.0.1:8080/todos", "[]", 5, {})\n'
            '  print(a.status + b.status + c.status)\n')
        self.assertEqual(errs(full(src)), [])

    def test_fetch_in_return_position(self):
        src = REQ_RESP + (
            "fn fetch(url: str) -> HttpResponse:\n"
            '  return http.get(url)\n'
            "\nfn main():\n"
            '  let r = fetch("http://127.0.0.1:8080/todos")\n'
            '  print(r.status)\n')
        self.assertEqual(errs(full(src)), [])

    def test_fetch_as_function_argument(self):
        src = REQ_RESP + TODO_STRUCT + (
            "fn show(resp: HttpResponse):\n"
            "  print(resp.status)\n"
            "\nfn main():\n"
            '  show(http.get("http://127.0.0.1:8080/todos"))\n')
        self.assertEqual(errs(full(src)), [])

    def test_computed_url_is_not_statically_checked(self):
        # A computed URL gets no static check (the runtime checks
        # it); the checker must stay quiet.
        src = main_body(
            '  let url = "http://127.0.0.1:8080" + "/todos"\n'
            '  let resp: HttpResponse = http.get(url)\n'
            '  print(resp.status)\n')
        self.assertEqual(errs(full(src)), [])

    def test_computed_method_and_path_are_not_statically_checked(self):
        src = main_body(
            '  let m = "GET"\n'
            '  let p = "/todos"\n'
            '  http.route(m, p, ' + INLINE_HANDLER + ')\n')
        self.assertEqual(errs(full(src)), [])

    def test_request_struct_by_shape_not_name(self):
        # A struct with a different name but exactly the five
        # HttpRequest fields is a valid request struct.
        src = """struct Req:
  method: str
  path: str
  headers: dict[str, str]
  body: str
  query: dict[str, str]

struct Resp:
  status: int
  headers: dict[str, str]
  body: str

fn main():
  http.route("GET", "/todos", fn(r: Req) -> Resp: Resp(status: 200, headers: {}, body: ""))
"""
        self.assertEqual(errs(full(src)), [])

    def test_fetch_sets_expr_type_for_emitters(self):
        src = main_body(
            '  let resp: HttpResponse = http.get('
            '"http://127.0.0.1:8080/todos")\n'
            '  print(resp)\n')
        resolved = resolve_source(src)
        check_program(resolved.program,
                      providers=discord_providers(resolved.program))
        check_http(resolved.program)
        found = []

        def walk(node):
            if isinstance(node, A.Call) and isinstance(node.func, A.Member) \
                    and node.func.name == "get":
                found.append(node)
            if isinstance(node, (A.Expr, A.Stmt)):
                for value in vars(node).values():
                    walk(value)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    walk(item)

        walk(resolved.program.functions[0].body)
        self.assertEqual(len(found), 1)
        self.assertEqual(str(found[0].expr_type), "HttpResponse")

    def test_localhost_url_passes_without_manifest(self):
        # The default allowlist is localhost-only; no manifest is
        # needed for a 127.0.0.1 fetch.
        src = main_body(
            '  let resp: HttpResponse = http.get('
            '"http://localhost:8080/todos")\n'
            '  print(resp.status)\n')
        self.assertEqual(errs(full(src)), [])

    def test_allowlisted_host_passes(self):
        src = main_body(
            '  let resp: HttpResponse = http.get('
            '"https://api.example.com/todos")\n'
            '  print(resp.status)\n')
        self.assertEqual(
            errs(full(src, allowlist=["api.example.com"])), [])


class TestHttpErrors(unittest.TestCase):
    def test_unknown_member(self):
        d = full(main_body('  http.fetch("http://127.0.0.1/")\n'))
        self.assertIn("E2012", codes(d))
        self.assertIn("http has no function `fetch`", messages(d))

    def test_unknown_member_suggests_similar_name(self):
        d = full(main_body('  http.gett("http://127.0.0.1/")\n'))
        self.assertIn("E2012", codes(d))
        self.assertTrue(any("`get`" in h for h in helps(d)))

    def test_named_argument_on_http_call(self):
        d = full(main_body(
            '  http.serve(port: 8080)\n'))
        self.assertIn("E2012", codes(d))

    def test_lowercase_method(self):
        d = full(main_body(
            '  http.route("get", "/todos", ' + INLINE_HANDLER + ')\n'))
        self.assertIn("E2012", codes(d))
        self.assertTrue(any("GET" in m for m in messages(d)))

    def test_unknown_method(self):
        d = full(main_body(
            '  http.route("FETCH", "/todos", ' + INLINE_HANDLER + ')\n'))
        self.assertIn("E2012", codes(d))

    def test_request_lowercase_method(self):
        d = full(main_body(
            '  let r: HttpResponse = http.request("put", '
            '"http://127.0.0.1/", "")\n'
            '  print(r.status)\n'))
        self.assertIn("E2012", codes(d))

    def test_path_without_leading_slash(self):
        d = full(main_body(
            '  http.route("GET", "todos", ' + INLINE_HANDLER + ')\n'))
        self.assertIn("E2012", codes(d))

    def test_duplicate_route(self):
        d = full(main_body(
            '  http.route("GET", "/todos", ' + INLINE_HANDLER + ')\n'
            '  http.route("GET", "/todos", ' + INLINE_HANDLER + ')\n'))
        self.assertIn("E2012", codes(d))
        self.assertTrue(any("duplicate route" in m for m in messages(d)))

    def test_same_path_different_method_is_not_a_duplicate(self):
        src = main_body(
            '  http.route("GET", "/todos", ' + INLINE_HANDLER + ')\n'
            '  http.route("POST", "/todos", ' + INLINE_HANDLER + ')\n')
        self.assertEqual(errs(full(src)), [])

    def test_named_function_handler(self):
        src = REQ_RESP + (
            "fn handle(req: HttpRequest) -> HttpResponse:\n"
            '  return HttpResponse(status: 200, headers: {}, body: "")\n'
            "\nfn main():\n"
            '  http.route("GET", "/todos", handle)\n')
        d = full(src)
        self.assertIn("E2012", codes(d))
        self.assertTrue(any("named function" in m for m in messages(d)))

    def test_handler_with_wrong_param_count(self):
        src = main_body(
            '  http.route("GET", "/todos", fn(a: HttpRequest, '
            'b: HttpRequest) -> HttpResponse: HttpResponse(status: 200, '
            'headers: {}, body: ""))\n')
        self.assertIn("E2012", codes(full(src)))

    def test_handler_param_struct_missing_field(self):
        src = """struct ShortReq:
  method: str
  path: str
  headers: dict[str, str]
  body: str

struct HttpResponse:
  status: int
  headers: dict[str, str]
  body: str

fn main():
  http.route("GET", "/todos", fn(r: ShortReq) -> HttpResponse: HttpResponse(status: 200, headers: {}, body: ""))
"""
        d = full(src)
        self.assertIn("E2012", codes(d))
        self.assertTrue(any("missing field `query:" in m
                            for m in messages(d)))

    def test_handler_param_struct_extra_field(self):
        src = """struct WideReq:
  method: str
  path: str
  headers: dict[str, str]
  body: str
  query: dict[str, str]
  extra: str

struct HttpResponse:
  status: int
  headers: dict[str, str]
  body: str

fn main():
  http.route("GET", "/todos", fn(r: WideReq) -> HttpResponse: HttpResponse(status: 200, headers: {}, body: ""))
"""
        d = full(src)
        self.assertIn("E2012", codes(d))
        self.assertTrue(any("extra field `extra`" in m
                            for m in messages(d)))

    def test_handler_param_struct_wrong_field_type(self):
        src = """struct BadReq:
  method: str
  path: int
  headers: dict[str, str]
  body: str
  query: dict[str, str]

struct HttpResponse:
  status: int
  headers: dict[str, str]
  body: str

fn main():
  http.route("GET", "/todos", fn(r: BadReq) -> HttpResponse: HttpResponse(status: 200, headers: {}, body: ""))
"""
        d = full(src)
        self.assertIn("E2012", codes(d))
        self.assertTrue(any("must be `str`" in m for m in messages(d)))

    def test_handler_return_struct_wrong(self):
        src = """struct HttpRequest:
  method: str
  path: str
  headers: dict[str, str]
  body: str
  query: dict[str, str]

struct ShortResp:
  status: int
  body: str

fn main():
  http.route("GET", "/todos", fn(r: HttpRequest) -> ShortResp: ShortResp(status: 200, body: ""))
"""
        d = full(src)
        self.assertIn("E2012", codes(d))
        self.assertTrue(any("missing field `headers:" in m
                            for m in messages(d)))

    def test_handler_without_return_type(self):
        src = main_body(
            '  http.route("GET", "/todos", fn(req: HttpRequest): '
            'HttpResponse(status: 200, headers: {}, body: ""))\n')
        # No `->` on the lambda: the body is a single expression, so
        # the parser reads it as the lambda body and the return type
        # is missing.
        d = full(src)
        self.assertIn("E2012", codes(d))

    def test_handler_param_is_enum(self):
        src = """enum Method:
  Get
  Post

struct HttpResponse:
  status: int
  headers: dict[str, str]
  body: str

fn main():
  http.route("GET", "/todos", fn(r: Method) -> HttpResponse: HttpResponse(status: 200, headers: {}, body: ""))
"""
        d = full(src)
        self.assertIn("E2012", codes(d))

    def test_handler_is_not_a_lambda(self):
        d = full(main_body(
            '  http.route("GET", "/todos", 42)\n'))
        self.assertIn("E2012", codes(d))

    def test_serve_port_wrong_type(self):
        d = full(main_body('  http.serve("8080")\n'))
        self.assertIn("E2012", codes(d))

    def test_serve_port_out_of_range(self):
        d = full(main_body('  http.serve(70000)\n'))
        self.assertIn("E2012", codes(d))

    def test_serve_arity(self):
        d = full(main_body('  http.serve()\n'))
        self.assertIn("E2012", codes(d))
        d = full(main_body('  http.serve(8080, "127.0.0.1", 1)\n'))
        self.assertIn("E2012", codes(d))

    def test_route_arity(self):
        d = full(main_body(
            '  http.route("GET", "/todos")\n'))
        self.assertIn("E2012", codes(d))

    def test_get_with_no_expected_type(self):
        d = full(main_body(
            '  let resp = http.get("http://127.0.0.1:8080/todos")\n'
            '  print(resp)\n'))
        self.assertIn("E2012", codes(d))
        self.assertTrue(any("needs an expected response type" in m
                            for m in messages(d)))

    def test_try_get_with_annotation(self):
        # M8b follow-up (dart agent find): `try http.get(...)` with
        # a Result annotation checks clean; the expected type is
        # read through the TryExpr.
        src = main_body(
            '  let tried: Result[HttpResponse, error] = '
            'try http.get("http://127.0.0.1:9/todos")\n'
            '  print(tried.is_ok())\n')
        self.assertEqual(errs(full(src)), [])

    def test_try_get_without_annotation(self):
        # No annotation means no expected type (E2012, correctly),
        # but never E2008: a named-module call is not a store
        # operation (discord_check _note_store_call skips
        # NAMED_MODULES).
        src = main_body(
            '  let tried = try http.get("http://127.0.0.1:9/todos")\n'
            '  print(tried.is_ok())\n')
        d = full(src)
        self.assertIn("E2012", codes(d))
        self.assertNotIn("E2008", codes(d))

    def test_get_with_wrong_expected_type(self):
        d = full(main_body(
            '  let resp: str = http.get("http://127.0.0.1:8080/todos")\n'
            '  print(resp)\n'))
        self.assertIn("E2012", codes(d))

    def test_get_with_enum_expected_type(self):
        src = """enum Status:
  Ok
  Bad

fn main():
  let resp: Status = http.get("http://127.0.0.1:8080/todos")
  print(resp)
"""
        d = full(src)
        self.assertIn("E2012", codes(d))

    def test_get_timeout_zero(self):
        d = full(main_body(
            '  let r: HttpResponse = http.get('
            '"http://127.0.0.1:8080/todos", 0)\n'
            '  print(r.status)\n'))
        self.assertIn("E2012", codes(d))

    def test_get_timeout_negative(self):
        d = full(main_body(
            '  let r: HttpResponse = http.get('
            '"http://127.0.0.1:8080/todos", -5)\n'
            '  print(r.status)\n'))
        self.assertIn("E2012", codes(d))

    def test_get_timeout_wrong_type(self):
        d = full(main_body(
            '  let r: HttpResponse = http.get('
            '"http://127.0.0.1:8080/todos", "5")\n'
            '  print(r.status)\n'))
        self.assertIn("E2012", codes(d))

    def test_get_headers_wrong_type(self):
        d = full(main_body(
            '  let r: HttpResponse = http.get('
            '"http://127.0.0.1:8080/todos", 5, {"X": 1})\n'
            '  print(r.status)\n'))
        self.assertIn("E2012", codes(d))

    def test_url_without_scheme(self):
        d = full(main_body(
            '  let r: HttpResponse = http.get("127.0.0.1:8080/todos")\n'
            '  print(r.status)\n'))
        self.assertIn("E2012", codes(d))

    def test_url_without_host(self):
        d = full(main_body(
            '  let r: HttpResponse = http.get("http:///todos")\n'
            '  print(r.status)\n'))
        self.assertIn("E2012", codes(d))

    def test_url_with_userinfo(self):
        d = full(main_body(
            '  let r: HttpResponse = http.get('
            '"http://user:pass@127.0.0.1:8080/todos")\n'
            '  print(r.status)\n'))
        self.assertIn("E2012", codes(d))
        self.assertTrue(any("userinfo" in m for m in messages(d)))

    def test_url_host_not_on_allowlist(self):
        d = full(main_body(
            '  let r: HttpResponse = http.get('
            '"https://api.example.com/todos")\n'
            '  print(r.status)\n'))
        self.assertIn("E2012", codes(d))
        self.assertTrue(any("allowlist" in m for m in messages(d)))

    def test_post_body_wrong_type(self):
        d = full(main_body(
            '  let r: HttpResponse = http.post('
            '"http://127.0.0.1:8080/todos", 42)\n'
            '  print(r.status)\n'))
        self.assertIn("E2012", codes(d))

    def test_post_arity(self):
        d = full(main_body(
            '  let r: HttpResponse = http.post('
            '"http://127.0.0.1:8080/todos")\n'
            '  print(r.status)\n'))
        self.assertIn("E2012", codes(d))

    def test_http_as_value(self):
        d = full(main_body('  print(http.get)\n'))
        self.assertIn("E2012", codes(d))

    def test_let_bound_non_lambda_handler(self):
        src = main_body(
            '  let h = 42\n'
            '  http.route("GET", "/todos", h)\n')
        d = full(src)
        self.assertIn("E2012", codes(d))

    def test_let_bound_lambda_handler_wrong_shape(self):
        src = main_body(
            '  let h = fn(req: str) -> HttpResponse: '
            'HttpResponse(status: 200, headers: {}, body: "")\n'
            '  http.route("GET", "/todos", h)\n')
        d = full(src)
        self.assertIn("E2012", codes(d))


class TestHttpTestBlocks(unittest.TestCase):
    """A test block is lowered to a synthetic `_newr_test_<n>`
    function sharing the body list: http_check must walk it once,
    via test_blocks, not twice (M8c follow-up, ported from
    ui_check.py)."""

    def test_route_in_test_block_is_not_a_duplicate(self):
        # Before the fix this read as a duplicate route: the
        # functions loop registered it, then the test_blocks walk
        # found it already registered.
        src = (REQ_RESP + 'test "route in test":\n'
               '  http.route("GET", "/todos", ' + INLINE_HANDLER
               + ')\n')
        self.assertEqual(errs(full(src)), [])

    def test_bad_method_in_test_block_reports_once(self):
        src = (REQ_RESP + 'test "bad method in test":\n'
               '  http.route("FETCH", "/todos", ' + INLINE_HANDLER
               + ')\n')
        diags = errs(full(src))
        self.assertEqual(len(diags), 1)
        self.assertEqual(codes(diags), ["E2012"])


class TestHttpCoreBoundary(unittest.TestCase):
    """The core checker never reports an http rule: it returns
    UNKNOWN for http calls, and http_check does the real work."""

    def test_core_alone_is_quiet_for_valid_http(self):
        src = main_body(
            '  http.route("GET", "/todos", ' + INLINE_HANDLER + ')\n'
            '  http.serve(8080)\n'
            '  let resp: HttpResponse = http.get('
            '"http://127.0.0.1:8080/todos")\n'
            '  print(resp.status)\n')
        self.assertEqual(errs(core_only(src)), [])

    def test_core_alone_is_quiet_for_invalid_http(self):
        # A lowercase method is E2012 from http_check only; core
        # must not report it (and must not report anything else
        # about the call either).
        src = main_body(
            '  http.route("get", "/todos", ' + INLINE_HANDLER + ')\n')
        self.assertEqual(errs(core_only(src)), [])
        self.assertIn("E2012", codes(full(src)))


class TestHttpDocs(unittest.TestCase):
    """C6: `newr docs` renders the http surface into docs/api.md with
    no omissions."""

    def test_every_http_member_is_documented(self):
        from compiler.docs import api_markdown
        from compiler.http_limits import HTTP_MEMBERS
        text = api_markdown()
        for name in HTTP_MEMBERS:
            self.assertIn(f"`http.{name}`", text, f"http.{name} missing")

    def test_every_http_member_has_doc_and_sig(self):
        from compiler.http_limits import HTTP_MEMBERS
        for name, entry in HTTP_MEMBERS.items():
            self.assertTrue(entry.get("doc"), f"http.{name} has no doc")
            self.assertTrue(entry.get("sig"), f"http.{name} has no sig")


if __name__ == "__main__":
    unittest.main()
