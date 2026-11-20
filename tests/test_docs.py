"""`///` doc comments and the `newr docs` generator (SPEC.md section 6,
section 19, section 24.2 idea 14; M6 Phase 3).

Doc comments are frozen in M6: a `///` line at the start of a logical
line documents the declaration or statement directly below it. The
lexer emits them as DOC_COMMENT tokens, the parser attaches the text
to the node below (as `node.doc`), and `newr fmt` keeps them in place
like `#` comments. Doc comments are syntax only: they change no
checking, resolution, or emitted code.

Rules pinned by these tests:

- A `///` line is a doc comment only at the start of a logical line.
  The same three slashes after code on a line stay three SLASH tokens
  (division), exactly as before.
- A doc comment attaches to the item directly below it. A blank line
  between the comment and the item breaks the attachment (the comment
  documents nothing, like Rust).
- `node.doc` is not semantic: it is excluded from AST equality, and a
  documented program checks, builds, and runs exactly like the same
  program without the comments.
- The generated API docs list every stdlib member in
  compiler/modules.py with no omissions (mechanical check), and every
  example program carries a `///` doc comment.
- Every ```newr sample in README.md and docs/*.md is a real program:
  it checks, builds for py, and runs.
"""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compiler import __version__  # noqa: E402
from compiler.docs import (  # noqa: E402
    api_markdown,
    examples_markdown,
    extract_newr_samples,
    generate_docs,
)
from compiler.fmt import format_source  # noqa: E402
from compiler.lexer import tokenize  # noqa: E402
from compiler.modules import BUILTINS, MODULES  # noqa: E402
from compiler.parser import parse  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"


def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(LAUNCHER), *args],
                          capture_output=True, text=True, cwd=cwd)


class TestLexerDocComments(unittest.TestCase):

    def test_doc_comment_token_at_line_start(self):
        toks = tokenize("/// Hello there\nfn f():\n  pass\n")
        docs = [t for t in toks if t.kind == "DOC_COMMENT"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].value, "Hello there")

    def test_doc_comment_without_space_after_slashes(self):
        toks = tokenize("///No space\nprint 1\n")
        docs = [t for t in toks if t.kind == "DOC_COMMENT"]
        self.assertEqual(docs[0].value, "No space")

    def test_doc_comment_keeps_indent_position(self):
        toks = tokenize("fn f():\n  /// Inside the body\n  pass\n")
        docs = [t for t in toks if t.kind == "DOC_COMMENT"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].value, "Inside the body")

    def test_slashes_after_code_are_still_division(self):
        toks = tokenize("let x: int = 12 /// 3\n")
        kinds = [t.kind for t in toks]
        self.assertNotIn("DOC_COMMENT", kinds)
        self.assertEqual(kinds.count("SLASH"), 3)

    def test_hash_comments_unchanged(self):
        toks = tokenize("# plain comment\nprint 1\n")
        self.assertNotIn("DOC_COMMENT", [t.kind for t in toks])
        content = [t for t in toks
                   if t.kind not in ("NEWLINE", "EOF")]
        self.assertEqual([(t.kind, t.value) for t in content],
                         [("IDENT", "print"), ("INT", 1)])


class TestParserDocComments(unittest.TestCase):

    def test_doc_attaches_to_function(self):
        prog = parse("/// Doubles a number.\nfn double(x: int) -> int:\n"
                     "  return x * 2\n")
        self.assertEqual(len(prog.functions), 1)
        self.assertEqual(prog.functions[0].doc, "Doubles a number.")

    def test_doc_attaches_to_pub_function(self):
        prog = parse("/// The price of an item.\n"
                     "pub fn price_of(item: str) -> int:\n  return 0\n")
        self.assertEqual(prog.functions[0].doc, "The price of an item.")
        self.assertEqual(prog.functions[0].name, "price_of")

    def test_multiline_doc_joins_with_newlines(self):
        prog = parse("/// First line.\n/// Second line.\n"
                     "fn f():\n  pass\n")
        self.assertEqual(prog.functions[0].doc,
                         "First line.\nSecond line.")

    def test_blank_line_breaks_attachment(self):
        prog = parse("/// Orphaned comment.\n\nfn f():\n  pass\n")
        self.assertIsNone(prog.functions[0].doc)

    def test_doc_attaches_to_struct_and_test_block(self):
        prog = parse("/// A point.\nstruct Point:\n  x: int\n\n"
                     "/// Checks doubling.\ntest \"doubles\":\n"
                     "  requires 1 == 1\n")
        self.assertEqual(prog.structs[0].doc, "A point.")
        self.assertEqual(prog.test_blocks[0].doc, "Checks doubling.")

    def test_doc_attaches_to_statement_inside_body(self):
        prog = parse("fn f():\n  /// Greet the world.\n  print \"hi\"\n")
        body = prog.functions[0].body
        self.assertEqual(body[0].doc, "Greet the world.")

    def test_doc_attaches_to_top_level_statement(self):
        prog = parse("/// Say hello.\nprint \"Hello, world!\"\n")
        self.assertEqual(prog.stmts[0].doc, "Say hello.")

    def test_doc_is_not_semantic(self):
        # The same program with and without doc comments builds to
        # byte-identical output: doc comments change no checking,
        # resolution, or emitted code.
        plain = "fn double(x: int) -> int:\n  return x * 2\n"
        documented = ("/// Doubles a number.\n"
                      "fn double(x: int) -> int:\n"
                      "  /// Double it.\n  return x * 2\n")
        with tempfile.TemporaryDirectory() as tmp:
            outputs = []
            for label, source in (("plain", plain),
                                  ("documented", documented)):
                # Same file name in both runs: the emitted header
                # embeds the source file name, which must not differ.
                sub = Path(tmp) / label
                sub.mkdir()
                src = sub / "demo.newr"
                src.write_text(source, encoding="utf-8")
                out = sub / "demo.py"
                result = run_cli("build", "--target", "py", "-o",
                                 str(out), str(src))
                self.assertEqual(result.returncode, 0,
                                 f"{label} fails build: {result.stderr}")
                outputs.append(out.read_text(encoding="utf-8")
                               .replace(label, "LABEL"))
            self.assertEqual(outputs[0], outputs[1])

    def test_undocumented_program_has_none_doc(self):
        prog = parse("fn f():\n  pass\n")
        self.assertIsNone(prog.functions[0].doc)

    def test_file_header_followed_by_blank_line(self):
        prog = parse("/// File header.\n/// Second line.\n\n"
                     "fn f():\n  pass\n")
        self.assertEqual(prog.file_doc, "File header.\nSecond line.")
        self.assertIsNone(prog.functions[0].doc)

    def test_block_directly_above_first_item_is_its_doc(self):
        prog = parse("/// Fn doc.\nfn f():\n  pass\n")
        self.assertIsNone(prog.file_doc)
        self.assertEqual(prog.functions[0].doc, "Fn doc.")

    def test_file_with_only_a_doc_block(self):
        prog = parse("/// Just a header.\n")
        self.assertEqual(prog.file_doc, "Just a header.")
        self.assertEqual(prog.functions, [])


class TestFmtDocComments(unittest.TestCase):

    def test_fmt_preserves_doc_comment(self):
        source = "/// Doubles a number.\nfn double(x: int) -> int:\n" \
                 "  return x * 2\n"
        self.assertEqual(format_source(source, "demo.newr"), source)

    def test_fmt_is_idempotent_with_doc_comments(self):
        source = "/// File doc.\n\n/// Fn doc.\nfn f():\n  pass\n"
        once = format_source(source, "demo.newr")
        self.assertEqual(format_source(once, "demo.newr"), once)

    def test_fmt_keeps_doc_above_item(self):
        source = "fn f():\n  /// Inside.\n  print 1\n"
        formatted = format_source(source, "demo.newr")
        self.assertIn("/// Inside.\n  print 1", formatted)


class TestVersion(unittest.TestCase):

    def test_version_is_0_2_0(self):
        self.assertEqual(__version__, "0.2.0")

    def test_cli_version_prints_0_2_0(self):
        result = run_cli("version")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "newr 0.2.0")

    def test_cli_version_json_envelope(self):
        result = run_cli("--json", "version")
        self.assertEqual(result.returncode, 0)
        envelope = json.loads(result.stdout)
        self.assertEqual(envelope["data"]["version"], "0.2.0")


class TestApiDocs(unittest.TestCase):

    def test_every_builtin_is_documented(self):
        text = api_markdown()
        for name in BUILTINS:
            self.assertIn(f"`{name}`", text, f"builtin {name} missing")

    def test_every_module_member_is_documented(self):
        text = api_markdown()
        for module, members in MODULES.items():
            for name in members:
                self.assertIn(f"`{module}.{name}`", text,
                              f"{module}.{name} missing")

    def test_every_member_has_doc_and_sig(self):
        for name, entry in BUILTINS.items():
            self.assertTrue(entry.get("doc"), f"builtin {name} has no doc")
            self.assertTrue(entry.get("sig"), f"builtin {name} has no sig")
        for module, members in MODULES.items():
            for name, entry in members.items():
                self.assertTrue(entry.get("doc"),
                                f"{module}.{name} has no doc")
                self.assertTrue(entry.get("sig"),
                                f"{module}.{name} has no sig")

    def test_api_docs_mention_section_12_methods(self):
        text = api_markdown()
        for method in ("upper()", "split(", "keys()", "is_some()",
                       "is_ok()"):
            self.assertIn(method, text, f"method {method} missing")

    def test_api_docs_are_deterministic(self):
        self.assertEqual(api_markdown(), api_markdown())


class TestExamplesDocs(unittest.TestCase):

    def test_every_example_has_a_doc_comment(self):
        examples = sorted((ROOT / "examples").glob("*.newr"))
        self.assertTrue(examples)
        for path in examples:
            text = path.read_text(encoding="utf-8")
            prog = parse(text, str(path))
            items = (prog.functions + prog.structs + prog.enums
                     + prog.test_blocks + prog.stmts)
            if prog.bot is not None:
                items = items + [prog.bot]
            items = items + prog.commands + prog.events
            docs = [node.doc for node in items if node.doc]
            self.assertTrue(prog.file_doc or docs,
                            f"{path.name} has no /// doc comment")

    def test_examples_markdown_lists_every_example(self):
        text = examples_markdown(ROOT)
        for path in sorted((ROOT / "examples").glob("*.newr")):
            self.assertIn(path.name, text, f"{path.name} missing")

    def test_generated_docs_match_committed_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = generate_docs(Path(tmp), ROOT)
            for name in written:
                generated = (Path(tmp) / name).read_text(encoding="utf-8")
                committed = (ROOT / "docs" / name).read_text(
                    encoding="utf-8")
                self.assertEqual(generated, committed,
                                 f"docs/{name} is stale, run `newr docs`")


class TestDocsCommand(unittest.TestCase):

    def test_docs_writes_api_and_examples(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_cli("docs", "--out", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(tmp) / "api.md").is_file())
            self.assertIn("api.md", result.stdout)

    def test_docs_json_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_cli("--json", "docs", "--out", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            envelope = json.loads(result.stdout)
            self.assertTrue(envelope["ok"])
            self.assertEqual(envelope["command"], "docs")

    def test_docs_help_names_the_command(self):
        result = run_cli("help", "docs")
        self.assertEqual(result.returncode, 0)
        self.assertIn("docs", result.stdout)


class TestDocSamplesRun(unittest.TestCase):
    """Every ```newr sample in README.md and docs/*.md runs."""

    def test_samples_check_build_and_run(self):
        files = [ROOT / "README.md"] + sorted((ROOT / "docs").glob("*.md"))
        samples = []
        for path in files:
            samples.extend(extract_newr_samples(path))
        self.assertTrue(samples, "no ```newr samples found")
        for path, sample in samples:
            with tempfile.TemporaryDirectory() as tmp:
                src = Path(tmp) / "sample.newr"
                src.write_text(sample, encoding="utf-8")
                checked = run_cli("check", str(src))
                self.assertEqual(checked.returncode, 0,
                                 f"{path} sample fails check: "
                                 f"{checked.stdout}{checked.stderr}")
                out = Path(tmp) / "sample.py"
                built = run_cli("build", "--target", "py", "-o", str(out),
                                str(src))
                self.assertEqual(built.returncode, 0,
                                 f"{path} sample fails build: "
                                 f"{built.stdout}{built.stderr}")
                ran = subprocess.run([sys.executable, str(out)],
                                     capture_output=True, text=True,
                                     cwd=tmp)
                self.assertEqual(ran.returncode, 0,
                                 f"{path} sample fails to run: {ran.stderr}")


if __name__ == "__main__":
    unittest.main()
