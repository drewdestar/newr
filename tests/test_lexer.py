"""Lexer tests (SPEC.md §6). Seams: tokenize(source, filename) -> tokens.

Tests verify behavior through the public interface only: the token kinds,
values, and spans for a source string.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compiler.errors import NewrError, line_col  # noqa: E402
from compiler.lexer import Token, tokenize  # noqa: E402


def kinds(src, filename="<test>"):
    return [t.kind for t in tokenize(src, filename)]


def values(src, filename="<test>"):
    return [(t.kind, t.value) for t in tokenize(src, filename)]


class TestBasicTokens(unittest.TestCase):
    def test_let_statement(self):
        toks = values("let x = 42\n")
        self.assertEqual(toks, [
            ("KEYWORD", "let"), ("IDENT", "x"), ("ASSIGN", "="),
            ("INT", 42), ("NEWLINE", "\n"), ("EOF", None),
        ])

    def test_keywords_are_keywords_not_idents(self):
        toks = tokenize("if elif else for while fn return let const\n")
        self.assertTrue(all(t.kind == "KEYWORD" for t in toks[:-2]))
        self.assertEqual([t.value for t in toks[:-2]],
                         ["if", "elif", "else", "for", "while", "fn",
                          "return", "let", "const"])

    def test_operators(self):
        toks = values("a + b - c * d / e % f ** g == h != i < j <= k > l >= m\n")
        ops = [v for k, v in toks if k not in ("IDENT", "NEWLINE", "EOF")]
        self.assertEqual(ops, ["+", "-", "*", "/", "%", "**", "==", "!=",
                               "<", "<=", ">", ">="])

    def test_augmented_assignment_and_arrow(self):
        toks = values("x += 1\ny -= 2\nz *= 3\nw /= 4\nfn f(a: int) -> int:\n")
        self.assertIn(("PLUS_EQ", "+="), toks)
        self.assertIn(("MINUS_EQ", "-="), toks)
        self.assertIn(("STAR_EQ", "*="), toks)
        self.assertIn(("SLASH_EQ", "/="), toks)
        self.assertIn(("ARROW", "->"), toks)

    def test_punctuation(self):
        toks = values("f(a, b)[0].x\n")
        self.assertEqual([k for k, _ in toks],
                         ["IDENT", "LPAREN", "IDENT", "COMMA", "IDENT",
                          "RPAREN", "LBRACK", "INT", "RBRACK", "DOT",
                          "IDENT", "NEWLINE", "EOF"])


class TestNumbers(unittest.TestCase):
    def test_int_float_hex_binary_underscores(self):
        toks = values("1 3.14 0xFF 0b1010 1_000 1e3 1.5e-2\n")
        self.assertEqual([v for _, v in toks if _ != "EOF" and _ != "NEWLINE"],
                         [1, 3.14, 255, 10, 1000, 1000.0, 0.015])

    def test_int_is_int_float_is_float(self):
        toks = tokenize("42 3.14\n")
        self.assertEqual(toks[0].kind, "INT")
        self.assertEqual(toks[1].kind, "FLOAT")

    def test_duration_literals(self):
        toks = values("60s 5m 1h 1d\n")
        self.assertEqual(toks, [
            ("DURATION", 60), ("DURATION", 300), ("DURATION", 3600),
            ("DURATION", 86400), ("NEWLINE", "\n"), ("EOF", None),
        ])

    def test_duration_needs_no_space_but_number_space_ident_is_not_duration(self):
        toks = values("1 h\n")
        self.assertEqual(toks[0], ("INT", 1))
        self.assertEqual(toks[1], ("IDENT", "h"))

    def test_bad_hex_is_error(self):
        with self.assertRaises(NewrError) as ctx:
            tokenize("0xZZ\n")
        self.assertIn("E_BAD_NUMBER", ctx.exception.code)

    def test_int_overflow_is_error(self):
        with self.assertRaises(NewrError) as ctx:
            tokenize("99999999999999999999999\n")
        self.assertIn("E_INT_OVERFLOW", ctx.exception.code)


class TestStrings(unittest.TestCase):
    def test_plain_string(self):
        toks = values('"hello"\n')
        self.assertEqual(toks, [
            ("STR_START", None), ("STR_PART", "hello"), ("STR_END", None),
            ("NEWLINE", "\n"), ("EOF", None),
        ])

    def test_escapes(self):
        toks = tokenize(r'"a\"b\\c\nd\te\rf\u{1F600}"' + "\n")
        part = [t for t in toks if t.kind == "STR_PART"][0]
        self.assertEqual(part.value, 'a"b\\c\nd\te\rf\U0001F600')

    def test_bad_escape_is_error(self):
        with self.assertRaises(NewrError) as ctx:
            tokenize(r'"a\q"' + "\n")
        self.assertIn("E_BAD_ESCAPE", ctx.exception.code)

    def test_unterminated_string_is_error(self):
        with self.assertRaises(NewrError) as ctx:
            tokenize('"abc\n')
        self.assertIn("E_UNTERMINATED_STRING", ctx.exception.code)

    def test_interpolation(self):
        toks = values('"Hi {user}, you have {x + 1} gold!"\n')
        self.assertEqual(toks, [
            ("STR_START", None), ("STR_PART", "Hi "),
            ("INTERP_START", "{"), ("IDENT", "user"), ("INTERP_END", "}"),
            ("STR_PART", ", you have "),
            ("INTERP_START", "{"), ("IDENT", "x"), ("PLUS", "+"),
            ("INT", 1), ("INTERP_END", "}"),
            ("STR_PART", " gold!"), ("STR_END", None),
            ("NEWLINE", "\n"), ("EOF", None),
        ])

    def test_brace_escapes(self):
        toks = tokenize('"{{not interp}} and {x}"\n')
        parts = [t.value for t in toks if t.kind == "STR_PART"]
        self.assertEqual(parts, ["{not interp} and "])

    def test_stray_close_brace_is_error(self):
        with self.assertRaises(NewrError) as ctx:
            tokenize('"a}b"\n')
        self.assertIn("E_STRAY_BRACE", ctx.exception.code)

    def test_unclosed_interpolation_is_error(self):
        with self.assertRaises(NewrError) as ctx:
            tokenize('"a {x"\n')
        self.assertIn("E_UNTERMINATED_STRING", ctx.exception.code)

    def test_nested_string_in_interpolation(self):
        toks = values('"a {f("b {y} c")} d"\n')
        kinds_list = [k for k, _ in toks]
        self.assertIn("STR_START", kinds_list)
        # inner string has its own STR_START ... STR_END inside the interp
        self.assertEqual(kinds_list.count("STR_START"), 2)
        self.assertEqual(kinds_list.count("INTERP_START"), 2)

    def test_dict_literal_inside_interpolation(self):
        # A dict literal inside interpolation needs a space after the
        # opening brace: "{{" is the escape for a literal "{" (SPEC.md §6),
        # so "{{" + dict is genuinely ambiguous and means a literal brace.
        toks = values('"a { {"k": 1}["k"] } b"\n')
        self.assertIn(("LBRACE", "{"), toks)
        self.assertIn(("RBRACE", "}"), toks)
        self.assertIn(("INTERP_END", "}"), toks)


class TestIndentation(unittest.TestCase):
    def test_indent_dedent(self):
        toks = values("if x:\n  y\n  z\nw\n")
        self.assertEqual([k for k, _ in toks],
                         ["KEYWORD", "IDENT", "COLON", "NEWLINE", "INDENT",
                          "IDENT", "NEWLINE", "IDENT", "NEWLINE", "DEDENT",
                          "IDENT", "NEWLINE", "EOF"])

    def test_nested_indent(self):
        toks = values("if a:\n  if b:\n    x\n  y\nz\n")
        self.assertEqual([k for k, _ in toks],
                         ["KEYWORD", "IDENT", "COLON", "NEWLINE", "INDENT",
                          "KEYWORD", "IDENT", "COLON", "NEWLINE", "INDENT",
                          "IDENT", "NEWLINE", "DEDENT", "IDENT", "NEWLINE",
                          "DEDENT", "IDENT", "NEWLINE", "EOF"])

    def test_blank_lines_and_comments_do_not_affect_indent(self):
        toks = values("if x:\n  # comment\n\n  y\nz\n")
        self.assertEqual([k for k, _ in toks],
                         ["KEYWORD", "IDENT", "COLON", "NEWLINE", "INDENT",
                          "IDENT", "NEWLINE", "DEDENT", "IDENT", "NEWLINE",
                          "EOF"])

    def test_tab_indent_is_error(self):
        with self.assertRaises(NewrError) as ctx:
            tokenize("if x:\n\ty\n")
        self.assertIn("E_TAB_INDENT", ctx.exception.code)

    def test_odd_indent_is_error(self):
        with self.assertRaises(NewrError) as ctx:
            tokenize("if x:\n   y\n")
        self.assertIn("E_BAD_INDENT", ctx.exception.code)

    def test_indent_jump_of_four_is_error(self):
        with self.assertRaises(NewrError) as ctx:
            tokenize("if x:\n    y\n")
        self.assertIn("E_BAD_INDENT", ctx.exception.code)

    def test_dedent_to_unknown_level_is_error(self):
        with self.assertRaises(NewrError) as ctx:
            tokenize("if a:\n  if b:\n    x\n y\n")
        self.assertIn("E_BAD_INDENT", ctx.exception.code)

    def test_indented_first_line_lexes_as_indent(self):
        # The lexer accepts an indented first line (it is a valid INDENT
        # step); the *parser* rejects it ("indented line outside any
        # block" - the v0 rule). See test_parser.py.
        toks = values("  x\n")
        self.assertEqual([k for k, _ in toks],
                         ["INDENT", "IDENT", "NEWLINE", "DEDENT", "EOF"])

    def test_bracket_continuation_ignores_newlines(self):
        toks = values("x = [\n  1,\n  2,\n]\n")
        # Exactly one NEWLINE: the one after the closing bracket.
        self.assertEqual([k for k, _ in toks].count("NEWLINE"), 1)
        # And no INDENT/DEDENT inside the brackets.
        self.assertNotIn("INDENT", [k for k, _ in toks])
        self.assertNotIn("DEDENT", [k for k, _ in toks])


class TestLineEndingsAndPositions(unittest.TestCase):
    def test_crlf_normalized(self):
        # Spans are RAW-file byte offsets (SPEC.md §16, M-06): a CRLF
        # newline is 2 bytes on disk, so line/col are derived from the
        # raw source - the CR byte is part of line 1.
        src = "let x = 1\r\nlet y = 2\r\n"
        toks = tokenize(src)
        self.assertEqual([t.kind for t in toks],
                         ["KEYWORD", "IDENT", "ASSIGN", "INT", "NEWLINE",
                          "KEYWORD", "IDENT", "ASSIGN", "INT", "NEWLINE",
                          "EOF"])
        # Line 1 is 11 raw bytes ("let x = 1\r\n"); the second `let`
        # starts at raw byte 11, line 2 column 1.
        self.assertEqual(line_col(src, toks[4].span.start_byte)[0], 1)
        self.assertEqual(line_col(src, toks[9].span.start_byte)[0], 2)
        self.assertEqual(line_col(src, toks[5].span.start_byte),
                         (2, 1))
        self.assertEqual(toks[5].span.start_byte, 11)

    def test_lone_cr_is_error(self):
        with self.assertRaises(NewrError) as ctx:
            tokenize("let x = 1\rlet y = 2\r")
        self.assertIn("E_BAD_LINE_ENDING", ctx.exception.code)

    def test_bom_stripped(self):
        toks = tokenize("\ufefflet x = 1\n")
        self.assertEqual(toks[0].kind, "KEYWORD")

    def test_spans_are_correct(self):
        # Spans are byte offsets; line/col come from the line_col helper.
        src = "let x = 42\n  y\n"
        toks = tokenize(src)
        let_tok = toks[0]
        self.assertEqual(line_col(src, let_tok.span.start_byte), (1, 1))
        self.assertEqual(line_col(src, let_tok.span.end_byte), (1, 4))
        int_tok = [t for t in toks if t.kind == "INT"][0]
        self.assertEqual(line_col(src, int_tok.span.start_byte), (1, 9))
        self.assertEqual(line_col(src, int_tok.span.end_byte), (1, 11))
        # And the byte offsets themselves: "let x = " is 8 bytes, "42" is 2.
        self.assertEqual((int_tok.span.start_byte, int_tok.span.end_byte),
                         (8, 10))

    def test_spans_are_byte_offsets_for_non_ascii(self):
        # A multi-byte character before the token shifts byte offsets
        # but not character columns (SPEC.md §16).
        src = "let s = \"é\"\nlet x = 42\n"
        toks = tokenize(src)
        int_tok = [t for t in toks if t.kind == "INT"][0]
        self.assertEqual(line_col(src, int_tok.span.start_byte), (2, 9))
        # Line 1 is 12 chars but 13 bytes ("é" is 2 bytes), so the int
        # starts at byte 13 + 8 = 21.
        self.assertEqual(int_tok.span.start_byte, 21)

    def test_error_carries_file_line_col(self):
        src = "let x = 1\nif y:\n\tx\n"
        try:
            tokenize(src, filename="foo.newr")
        except NewrError as exc:
            self.assertEqual(exc.span.file, "foo.newr")
            self.assertEqual(line_col(src, exc.span.start_byte), (3, 1))
        else:
            self.fail("expected NewrError")


if __name__ == "__main__":
    unittest.main()


class TestNewTokens(unittest.TestCase):
    def test_question_tokens(self):
        toks = values("a ? b : c\n")
        self.assertIn(("QMARK", "?"), toks)
        toks = values("a?.b\n")
        self.assertEqual([k for k, _ in toks],
                         ["IDENT", "QDOT", "IDENT", "NEWLINE", "EOF"])
        toks = values("a ?? b\n")
        self.assertIn(("QQ", "??"), toks)

    def test_two_char_forms_win_over_single_qmark(self):
        # "?." and "??" must not lex as QMARK followed by "." / "?".
        toks = values("x?.y ?? z\n")
        self.assertEqual([k for k, _ in toks],
                         ["IDENT", "QDOT", "IDENT", "QQ", "IDENT",
                          "NEWLINE", "EOF"])

    def test_pipe_token(self):
        toks = values("a |> f\n")
        self.assertEqual([k for k, _ in toks],
                         ["IDENT", "PIPE", "IDENT", "NEWLINE", "EOF"])
        self.assertIn(("PIPE", "|>"), toks)

    def test_bar_token_is_the_or_pattern_separator(self):
        # A lone "|" lexes as BAR (the match or-pattern separator,
        # SPEC.md §9.6); the parser rejects it anywhere else.
        toks = values("1 | 2\n")
        self.assertIn(("BAR", "|"), toks)

    def test_new_keywords(self):
        toks = tokenize("enum match pub requires ensures\n")
        self.assertTrue(all(t.kind == "KEYWORD" for t in toks[:-2]))
        self.assertEqual([t.value for t in toks[:-2]],
                         ["enum", "match", "pub", "requires", "ensures"])


class TestLeadingComments(unittest.TestCase):
    def test_leading_comment_and_blank_lines_lex(self):
        # Regression: a file starting with a comment (or blank lines)
        # used to hang the lexer in _process_indent.
        toks = values("# hello\n\n# more\nlet x = 1\n")
        # One NEWLINE token is emitted for the leading comment block.
        self.assertEqual([k for k, _ in toks],
                         ["NEWLINE", "KEYWORD", "IDENT", "ASSIGN", "INT",
                          "NEWLINE", "EOF"])
