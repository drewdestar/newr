"""Seeded corpus generator for newr stress testing.

Writes programs under stress/corpus/ plus manifest.json describing each
case: {name, category, expected, notes}. `expected` is the *intent* of
the case (valid / error / crash-candidate), NOT a verdict - the harness
records the actual outcome and the report flags mismatches.

Usage: python3 gen_corpus.py [--out DIR] [--seed N]
"""
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "corpus")


# ---------------------------------------------------------------- valid
def v_hello(rng):
    return 'print "Hello, world!"\n'


def v_arith(rng):
    a, b = rng.randint(-1000, 1000), rng.randint(1, 100)
    return (
        f"let a = {a}\nlet b = {b}\n"
        f"print a + b\nprint a - b\nprint a * b\n"
        f"print a / b\nprint a % b\nprint a ** 2\n"
        f"print -a\nprint a == b\nprint a < b\n"
    )


def v_fn(rng):
    return (
        "fn add(a: int, b: int) -> int:\n"
        "  return a + b\n"
        "fn greet(name: str) -> str:\n"
        '  return "Hello, {name}!"\n'
        "print add(40, 2)\n"
        'print greet("newr")\n'
        "fn fact(n: int) -> int:\n"
        "  if n <= 1:\n"
        "    return 1\n"
        "  return n * fact(n - 1)\n"
        "print fact(10)\n"
    )


def v_control(rng):
    return (
        "let n = 0\n"
        "while n < 5:\n"
        "  n = n + 1\n"
        "  if n == 3:\n"
        '    print "three"\n'
        "  elif n == 4:\n"
        '    print "four"\n'
        "  else:\n"
        "    print n\n"
        "for i in range(3):\n"
        "  print i\n"
        "for ch in \"abc\":\n"
        "  print ch\n"
    )


def v_strings(rng):
    return (
        'let who = "world"\n'
        'print "Hello, {who}!"\n'
        'print "sum={1 + 2}!"\n'
        'let s = "  padded  "\n'
        "print s.strip()\n"
        'print s.upper()\n'
        'print s.lower()\n'
        'print "abc".contains("b")\n'
        'print "a,b,c".split(",")\n'
        'print "x".join(["a", "b"])\n'
        'print "tab\\tnl\\nquote\\"end"\n'
        'print "emoji 🎉 ok"\n'
    )


def v_collections(rng):
    return (
        "let xs = [3, 1, 2]\n"
        "xs.append(4)\n"
        "print len(xs)\n"
        "print xs[0]\n"
        "print xs.get(9, -1)\n"
        "print xs.contains(2)\n"
        'let d = {"a": 1, "b": 2}\n'
        'print d["a"]\n'
        'print d.get("zz", 0)\n'
        "print d.keys()\n"
        "let st = set([1, 2, 2, 3])\n"
        "st.add(4)\n"
        "print st.contains(3)\n"
        "print len(st)\n"
    )


def v_nullable(rng):
    return (
        "let a: int? = 5\n"
        "let b: int? = none\n"
        "let c: str? = \"hi\"\n"
        "print a ?? 0\n"
        "print b ?? 42\n"
        "print c?.upper()\n"
        "fn maybe() -> int?:\n"
        "  return 7\n"
        "print maybe() ?? -1\n"
        "let d: int? = a ?? b\n"
        "print d ?? -9\n"
        "let e = a ?? b ?? 99\n"
        "print e\n"
    )


def v_contracts(rng):
    return (
        "fn withdraw(balance: int, amount: int) -> int:\n"
        "  requires amount > 0\n"
        "  requires amount <= balance\n"
        "  ensures result >= 0\n"
        "  return balance - amount\n"
        "print withdraw(100, 30)\n"
    )


def v_try(rng):
    return (
        "try:\n"
        '  let content = file.read("nope.txt")\n'
        "  print content\n"
        "catch err:\n"
        '  log "read failed"\n'
        "  print err\n"
        "finally:\n"
        '  log "done"\n'
        "fn boom() -> int:\n"
        '  throw "kaboom"\n'
        "try:\n"
        "  print boom()\n"
        "catch err:\n"
        '  print "caught"\n'
    )


def v_match(rng):
    return (
        "let v = 2\n"
        "match v:\n"
        "  1:\n"
        '    print "one"\n'
        "  2:\n"
        '    print "two"\n'
        "  _:\n"
        '    print "other"\n'
        "let w: int? = 5\n"
        "match w:\n"
        "  none:\n"
        '    print "none"\n'
        "  _:\n"
        '    print "some"\n'
    )


def v_struct_enum(rng):
    return (
        "struct Point:\n"
        "  x: int\n"
        "  y: int\n"
        "enum Color:\n"
        "  Red\n"
        "  Green\n"
        "let p = Point(x: 1, y: 2)\n"
        "print p.x + p.y\n"
        "let c = Color.Red\n"
        "print c\n"
    )


def v_numbers(rng):
    return (
        "print 0\n"
        "print 9223372036854775807\n"
        "print -9223372036854775808\n"
        "print 0x7FFFFFFFFFFFFFFF\n"
        "print 0b1010\n"
        "print 1_000_000\n"
        "print 3.14\n"
        "print 1.5e10\n"
        "print 1 / 2\n"
    )


def v_unicode_idents(rng):
    # Thai + Devanagari combining marks lex inside identifiers (spec).
    return (
        "let คะแนน = 10\n"
        "print คะแนน\n"
        "let Größe = 3\n"
        "print Größe\n"
        "let café = 1\n"
        "print café\n"
        'let s = "héllo 🎉 مرحبا"\n'
        "print s\n"
        "print len(s)\n"
    )


def v_stdlib(rng):
    return (
        "print math.sqrt(16.0)\n"
        "print math.floor(3.7)\n"
        "print math.pi\n"
        "print random.int(1, 10)\n"
        "print time.now()\n"
        "print time.format(time.now(), \"%Y-%m-%d\")\n"
        "print env.get(\"HOME\") ?? \"no-home\"\n"
        "print env.args\n"
        "print min(3, 1)\n"
        "print max(3, 1)\n"
        "print abs(-5)\n"
        "print round(3.6)\n"
        "print str(42)\n"
        "print int(\"7\")\n"
        "print float(\"2.5\")\n"
        "print bool(\"true\")\n"
    )


def v_ternary_pipe(rng):
    return (
        "let x = true ? 1 : 2\n"
        "print x\n"
        "let y = false ? 1 : 2\n"
        "print y\n"
        "let z = (1 + 2) * 3\n"
        "print z\n"
    )


def v_bot_minimal(rng):
    return (
        'bot "MiniBot":\n'
        '  description: "minimal"\n'
        "\n"
        "command ping:\n"
        '  description: "Answer with pong"\n'
        '  reply: "Pong! Hello {user}"\n'
        "\n"
        "event ready:\n"
        '  log: "{bot} is online"\n'
    )


def v_task_durations(rng):
    return (
        'bot "T":\n'
        '  description: "x"\n'
        "\n"
        "task every 1h:\n"
        "  do:\n"
        '    log "hourly tick"\n'
        "task every 60s:\n"
        "  do:\n"
        '    log "minutely tick"\n'
    )


# -------------------------------------------------------------- invalid
def i_unterminated_string(rng):
    return 'print "never closed\n'


def i_stray_brace(rng):
    return 'print "oops}"\n'


def i_bad_escape(rng):
    return 'print "bad \\q escape"\n'


def i_tab_indent(rng):
    return "if true:\n\tprint \"tabbed\"\n"


def i_bad_indent(rng):
    return "if true:\n   print \"three spaces\"\n"


def i_missing_colon(rng):
    return "if true\n  print \"no colon\"\n"


def i_bad_number(rng):
    return "print 1__2\n"


def i_unexpected_char(rng):
    return "print 1 @ 2\n"


def i_type_mismatch(rng):
    return 'let x: int = "not an int"\n'


def i_wrong_argc(rng):
    return (
        "fn f(a: int) -> int:\n"
        "  return a\n"
        "print f(1, 2)\n"
    )


def i_undefined_name(rng):
    return "print nosuchvar\n"


def i_use_before_def(rng):
    return (
        "print later\n"
        "let later = 1\n"
    )


def i_redefine_builtin(rng):
    return "let t = 1\n"


def i_duplicate_top(rng):
    return (
        "let dup = 1\n"
        "let dup = 2\n"
    )


def i_bad_nullable_recv(rng):
    # M-10: ?. on a non-nullable receiver must be an error, not silent ok.
    return 'print "hi"?.upper()\n'


def i_overflow_decimal(rng):
    return "print 9223372036854775808\n"


def i_overflow_hex(rng):
    return "print 0x8000000000000000\n"


def i_overflow_binary(rng):
    return "print 0b" + "1" * 65 + "\n"


def i_int64_min_ok(rng):
    # M-08: this one is VALID after the fix (representable).
    return "print -9223372036854775808\n"


def i_qq_nullable_rhs(rng):
    # M-09: T? ?? T? must work after the fix.
    return (
        "let a: str? = none\n"
        "let b: str? = \"fallback\"\n"
        "print a ?? b\n"
    )


def i_nullable_assign(rng):
    # M-03: T assignable to T? after the fix.
    return "let x: int? = 5\nprint x\n"


def i_div_zero(rng):
    # division by zero is a RUNTIME error (SPEC: int division by zero =
    # error at runtime) -- the checker correctly passes it.
    return "print 1 / 0\n"


def i_index_oob_type(rng):
    return 'print [1, 2]["zero"]\n'


def i_call_non_fn(rng):
    return (
        "let n = 5\n"
        "print n(1)\n"
    )


def i_return_outside_fn(rng):
    return "return 1\n"


def i_break_outside_loop(rng):
    return "break\n"


# -------------------------------------------------------------- discord
def d_desc_101(rng):
    return (
        'bot "B":\n'
        '  description: "x"\n'
        "command c:\n"
        '  description: "' + "d" * 101 + '"\n'
        '  reply: "hi"\n'
    )


def d_desc_100_ok(rng):
    return (
        'bot "B":\n'
        '  description: "x"\n'
        "command c:\n"
        '  description: "' + "d" * 100 + '"\n'
        '  reply: "hi"\n'
    )


def d_reply_2001(rng):
    return (
        'bot "B":\n'
        '  description: "x"\n'
        "command c:\n"
        '  reply: "' + "r" * 2001 + '"\n'
    )


def d_reply_2000_ok(rng):
    return (
        'bot "B":\n'
        '  description: "x"\n'
        "command c:\n"
        '  reply: "' + "r" * 2000 + '"\n'
    )


def d_reply_placeholders_ok(rng):
    # {user} expands to 22, {guild} to 100: 1978 + 22 = 2000 exactly.
    return (
        'bot "B":\n'
        '  description: "x"\n'
        "command c:\n"
        '  reply: "' + "r" * 1978 + '{user}"\n'
    )


def d_reply_placeholders_over(rng):
    # 1979 + 22 = 2001 -> must fail.
    return (
        'bot "B":\n'
        '  description: "x"\n'
        "command c:\n"
        '  reply: "' + "r" * 1979 + '{user}"\n'
    )


def d_cmd_name_33(rng):
    return (
        'bot "B":\n'
        '  description: "x"\n'
        "command " + "a" * 33 + ":\n"
        '  reply: "hi"\n'
    )


def d_cmd_name_32_ok(rng):
    return (
        'bot "B":\n'
        '  description: "x"\n'
        "command " + "a" * 32 + ":\n"
        '  reply: "hi"\n'
    )


def d_cmd_name_bad_chars(rng):
    return (
        'bot "B":\n'
        '  description: "x"\n'
        "command BadName:\n"
        '  reply: "hi"\n'
    )


def d_bot_name_33(rng):
    return 'bot "' + "B" * 33 + '":\n'


def d_dup_command(rng):
    return (
        'bot "B":\n'
        '  description: "x"\n'
        "command ping:\n"
        '  reply: "one"\n'
        "command ping:\n"
        '  reply: "two"\n'
    )


def d_unknown_intent(rng):
    return (
        'bot "B":\n'
        "  intents: [guilds, nosuchintent]\n"
        "command ping:\n"
        '  reply: "hi"\n'
    )


def d_option_min_max(rng):
    return (
        'bot "B":\n'
        '  description: "x"\n'
        "command buy:\n"
        "  option amount: int (min: 99, max: 1)\n"
        "  do:\n"
        '    reply "ok"\n'
    )


def d_too_many_options(rng):
    opts = "\n".join(
        f"  option o{i}: str" for i in range(26)
    )
    return (
        'bot "B":\n'
        '  description: "x"\n'
        "command big:\n"
        + opts
        + "\n  do:\n"
        '    reply "ok"\n'
    )


def d_no_answer_path(rng):
    return (
        'bot "B":\n'
        '  description: "x"\n'
        "command c:\n"
        "  do:\n"
        "    let x = 1\n"
    )


def d_embed_too_big(rng):
    return (
        'bot "B":\n'
        '  description: "x"\n'
        "command c:\n"
        "  do:\n"
        "    embed:\n"
        '      title: "' + "t" * 257 + '"\n'
        '    reply "hi"\n'
    )


# -------------------------------------------------------------- bytes
BYTES_CASES = {
    # name: (bytes, expected, notes)
    "b_nonutf8": (
        b'print "hi"\n\xff\xfe garbage\n', "error",
        "non-UTF-8 bytes must be a clean env error naming the file (M-02)",
    ),
    "b_empty": (b"", "valid", "empty file"),
    "b_only_comments": (b"# nothing here\n# just comments\n", "valid", "comments only"),
    "b_lone_cr": (b'print "a"\rprint "b"\n', "error",
                  "lone CR must be E_BAD_LINE_ENDING (M-07)"),
    "b_crlf_error": (
        b'print "ok"\r\nprint nosuchvar\r\n', "error",
        "CRLF file, error on line 2 - JSON offsets must match raw bytes (M-06)",
    ),
    "b_mixed_endings": (b'print "a"\r\nprint "b"\rprint "c"\n', "error",
                        "mixed CRLF + lone CR"),
    "b_bom": (b'\xef\xbb\xbfprint "bom ok"\n', "valid", "UTF-8 BOM stripped"),
    "b_truncated_string": (b'print "abc', "error", "EOF inside string"),
    "b_truncated_block": (b"if true:\n", "error", "EOF after colon, empty block"),
    "b_truncated_fn": (b"fn f(a: int) -> int:\n  return", "error",
                       "EOF mid-return"),
    "b_cesu8_surrogate": (b'print "\xed\xa0\x80"\n', "error",
                          "CESU-8 lone surrogate bytes - must not crash"),
    "b_nul_byte": (b'print "a\x00b"\n', "error",
                   "NUL byte inside string"),
    "b_nul_outside": (b'print 1\x00\n', "error", "NUL byte outside string"),
    "b_only_ws": (b"   \n\n  \n", "valid", "whitespace only"),
    "b_crlf_ok": (b'print "a"\r\nprint "b"\r\n', "valid", "CRLF valid file"),
    "b_cr_only_file": (b'print "a"\r', "error", "file is one lone-CR line"),
}


# -------------------------------------------------------------- depth
def depth_parens(n):
    return "print " + "("*n + "1" + ")"*n + "\n"


def depth_if(n):
    lines = []
    for _ in range(n):
        lines.append("if true:")
    lines.append('print "deep"')
    # indent: each level +2 spaces
    out = []
    for i, ln in enumerate(lines):
        out.append("  " * i + ln)
    return "\n".join(out) + "\n"


def depth_binop(n):
    return "print " + " + ".join(["1"] * n) + "\n"


def depth_calls(n):
    return "print " + "str(" * n + '"x"' + ")" * n + "\n"


def depth_list(n):
    return "print " + "[" * n + "1" + "]" * n + "\n"


def depth_dict(n):
    inner = '"k": 1'
    for _ in range(n):
        inner = '"k": {' + inner + '}'
    return "print {" + inner + "}\n"


def depth_unary(n):
    return "print " + "-" * n + "1\n"


def depth_ternary(n):
    expr = "1"
    for _ in range(n):
        expr = f"true ? {expr} : 2"
    return "print " + expr + "\n"


def depth_blocks(n):
    # nested `if true:` blocks each containing a let
    lines = []
    for i in range(n):
        lines.append("  " * i + "if true:")
    lines.append("  " * n + 'print "bottom"')
    return "\n".join(lines) + "\n"


def depth_interp(n):
    expr = "1"
    for _ in range(n):
        expr = f"true ? {expr} : 2"
    return 'print "{' + expr + '}"\n'


DEPTH_BUILDERS = {
    "parens": depth_parens,
    "if": depth_if,
    "binop": depth_binop,
    "calls": depth_calls,
    "list": depth_list,
    "dict": depth_dict,
    "unary": depth_unary,
    "ternary": depth_ternary,
    "blocks": depth_blocks,
    "interp": depth_interp,
}


# -------------------------------------------------------------- scale
def scale_lines(n=10000):
    return "".join(f'print "line {i}"\n' for i in range(n))


def scale_vars(n=10000):
    return "".join(f"let v{i} = {i}\n" for i in range(n)) + "print v9999\n"


def scale_string(nbytes=1_000_000):
    return 'print "' + "a" * nbytes + '"\n'


def scale_list(n=100000):
    return "print [" + ", ".join(str(i) for i in range(n)) + "]\n"


def scale_fns(n=1000):
    out = []
    for i in range(n):
        out.append(f"fn f{i}(x: int) -> int:")
        out.append(f"  return x + {i}")
    out.append("print f999(1)")
    return "\n".join(out) + "\n"


def scale_long_ident(n=10000):
    return "let " + "a" * n + " = 1\nprint " + "a" * n + "\n"


def scale_long_line(n=100000):
    return "print " + "1 + " * (n // 4) + "1\n"


def scale_bot_commands(n=500):
    out = ['bot "BigBot":', '  description: "x"']
    for i in range(n):
        out.append(f"command c{i}:")
        out.append(f'  reply: "reply {i}"')
    return "\n".join(out) + "\n"


# -------------------------------------------------------------- assembly
VALID = [
    ("v_hello", v_hello, "valid"),
    ("v_arith", v_arith, "valid"),
    ("v_fn", v_fn, "valid"),
    ("v_control", v_control, "valid"),
    ("v_strings", v_strings, "valid"),
    ("v_collections", v_collections, "valid"),
    ("v_nullable", v_nullable, "valid"),
    ("v_contracts", v_contracts, "valid"),
    ("v_try", v_try, "valid"),
    ("v_match", v_match, "valid"),
    ("v_struct_enum", v_struct_enum, "valid"),
    ("v_numbers", v_numbers, "valid"),
    ("v_unicode_idents", v_unicode_idents, "valid"),
    ("v_stdlib", v_stdlib, "valid"),
    ("v_ternary_pipe", v_ternary_pipe, "valid"),
    ("v_bot_minimal", v_bot_minimal, "valid"),
    ("v_task_durations", v_task_durations, "valid"),
]

INVALID = [
    ("i_unterminated_string", i_unterminated_string, "error"),
    ("i_stray_brace", i_stray_brace, "error"),
    ("i_bad_escape", i_bad_escape, "error"),
    ("i_tab_indent", i_tab_indent, "error"),
    ("i_bad_indent", i_bad_indent, "error"),
    ("i_missing_colon", i_missing_colon, "error"),
    ("i_bad_number", i_bad_number, "error"),
    ("i_unexpected_char", i_unexpected_char, "error"),
    ("i_type_mismatch", i_type_mismatch, "error"),
    ("i_wrong_argc", i_wrong_argc, "error"),
    ("i_undefined_name", i_undefined_name, "error"),
    ("i_use_before_def", i_use_before_def, "error"),
    ("i_redefine_builtin", i_redefine_builtin, "error"),
    ("i_duplicate_top", i_duplicate_top, "error"),
    ("i_bad_nullable_recv", i_bad_nullable_recv, "error"),
    ("i_overflow_decimal", i_overflow_decimal, "error"),
    ("i_overflow_hex", i_overflow_hex, "error"),
    ("i_overflow_binary", i_overflow_binary, "error"),
    ("i_int64_min_ok", i_int64_min_ok, "valid"),
    ("i_qq_nullable_rhs", i_qq_nullable_rhs, "valid"),
    ("i_nullable_assign", i_nullable_assign, "valid"),
    ("i_div_zero", i_div_zero, "valid"),  # runtime error, checker passes (see fn note)
    ("i_index_oob_type", i_index_oob_type, "error"),
    ("i_call_non_fn", i_call_non_fn, "error"),
    ("i_return_outside_fn", i_return_outside_fn, "error"),
    ("i_break_outside_loop", i_break_outside_loop, "error"),
]

DISCORD = [
    ("d_desc_101", d_desc_101, "error"),
    ("d_desc_100_ok", d_desc_100_ok, "valid"),
    ("d_reply_2001", d_reply_2001, "error"),
    ("d_reply_2000_ok", d_reply_2000_ok, "valid"),
    ("d_reply_placeholders_ok", d_reply_placeholders_ok, "valid"),
    ("d_reply_placeholders_over", d_reply_placeholders_over, "error"),
    ("d_cmd_name_33", d_cmd_name_33, "error"),
    ("d_cmd_name_32_ok", d_cmd_name_32_ok, "valid"),
    ("d_cmd_name_bad_chars", d_cmd_name_bad_chars, "error"),
    ("d_bot_name_33", d_bot_name_33, "error"),
    ("d_dup_command", d_dup_command, "error"),
    ("d_unknown_intent", d_unknown_intent, "error"),
    ("d_option_min_max", d_option_min_max, "error"),
    ("d_too_many_options", d_too_many_options, "error"),
    ("d_no_answer_path", d_no_answer_path, "error"),
    ("d_embed_too_big", d_embed_too_big, "error"),
]


def build(outdir=OUT, seed=20261106):
    rng = random.Random(seed)
    os.makedirs(outdir, exist_ok=True)
    manifest = []
    for name, fn, expected in VALID + INVALID + DISCORD:
        src = fn(rng)
        path = os.path.join(outdir, name + ".newr")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src)
        manifest.append({"name": name, "file": name + ".newr",
                         "category": name.split("_")[0],
                         "expected": expected, "bytes": False})
    for name, (data, expected, notes) in BYTES_CASES.items():
        path = os.path.join(outdir, name + ".newr")
        with open(path, "wb") as fh:
            fh.write(data)
        manifest.append({"name": name, "file": name + ".newr",
                         "category": "bytes", "expected": expected,
                         "bytes": True, "notes": notes})
    # import graph: chain of 20 files + star of 20 files
    impdir = os.path.join(outdir, "imports")
    os.makedirs(impdir, exist_ok=True)
    chain = []
    for i in range(20):
        nxt = f'import "imp{i + 1}.newr"\n' if i < 19 else ""
        src = nxt + f"pub let chain_val_{i} = {i}\n"
        with open(os.path.join(impdir, f"imp{i}.newr"), "w") as fh:
            fh.write(src)
        chain.append(f"imp{i}.newr")
    with open(os.path.join(impdir, "main_chain.newr"), "w") as fh:
        fh.write('import "imp0.newr"\nimport "imp19.newr"\nprint imp19.chain_val_19\n')
    manifest.append({"name": "imports_chain20", "file": "imports/main_chain.newr",
                     "category": "imports", "expected": "valid",
                     "notes": "20-file import chain, value from the last file"})
    # import cycle: a <-> b
    with open(os.path.join(impdir, "cyc_a.newr"), "w") as fh:
        fh.write('import "cyc_b.newr"\nlet a_val = 1\n')
    with open(os.path.join(impdir, "cyc_b.newr"), "w") as fh:
        fh.write('import "cyc_a.newr"\nlet b_val = 2\n')
    with open(os.path.join(impdir, "main_cycle.newr"), "w") as fh:
        fh.write('import "cyc_a.newr"\nprint a_val\n')
    manifest.append({"name": "imports_cycle", "file": "imports/main_cycle.newr",
                     "category": "imports", "expected": "error",
                     "notes": "import cycle a<->b must be a clean error"})
    # self import
    with open(os.path.join(impdir, "self.newr"), "w") as fh:
        fh.write('import "self.newr"\nprint 1\n')
    manifest.append({"name": "imports_self", "file": "imports/self.newr",
                     "category": "imports", "expected": "error",
                     "notes": "self-import must be a clean error"})
    # missing import
    with open(os.path.join(impdir, "main_missing.newr"), "w") as fh:
        fh.write('import "does_not_exist.newr"\nprint 1\n')
    manifest.append({"name": "imports_missing", "file": "imports/main_missing.newr",
                     "category": "imports", "expected": "error",
                     "notes": "missing import target"})
    with open(os.path.join(outdir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"wrote {len(manifest)} cases to {outdir}")
    return manifest


if __name__ == "__main__":
    outdir = OUT
    seed = 20261106
    args = sys.argv[1:]
    if "--out" in args:
        outdir = args[args.index("--out") + 1]
    if "--seed" in args:
        seed = int(args[args.index("--seed") + 1])
    build(outdir, seed)
