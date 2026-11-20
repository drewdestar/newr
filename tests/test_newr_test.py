"""Tests for `newr test` (SPEC.md section 19, section 24.2 idea 13,
M6 Phase 2c).

Two test forms share one runner: `fn test_*()` functions
(discovered by the `test_` name prefix) and `test "display name":`
blocks (SPEC.md section 8.7), in source order across both forms.
A test asserts with the language's own machinery (`if ...: throw
"message"` or a leading `requires`). The runner appends a
try/catch harness to the program's top-level statements, emits it
to a temporary directory, runs it through one target's emitter,
and reads the markers back out of stdout.

End-to-end tests go through the real `./newr` launcher via
subprocess (like tests/test_init.py). The py target is used for
speed; the seven-target run is verified manually per the M6 bar
(the conformance program and the examples run on all seven).
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compiler import ast_nodes as A  # noqa: E402
from compiler.test import (  # noqa: E402
    TestEnvError,
    collect_paths,
    discover_tests,
    exit_code,
    format_human,
    is_bot_program,
    parse_markers,
    run_test_file,
    synthesize_harness,
    to_json_data,
    validate_tests,
)
from compiler.resolver import resolve_source  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"

PASSING = """\
fn add(a: int, b: int) -> int:
  return a + b

fn test_add():
  if add(1, 2) != 3:
    throw "add(1, 2) should be 3"

fn test_add_requires():
  requires add(2, 2) == 4

fn helper():
  print "not a test"
"""

FAILING = """\
fn test_bad():
  if 1 + 1 != 3:
    throw "one plus one should be three"

fn test_good():
  if 1 + 1 == 2:
    print "inside good"
"""

NO_TESTS = """\
fn add(a: int, b: int) -> int:
  return a + b

print "hello"
"""

WITH_PARAMS = """\
fn test_with_param(x: int):
  if x != 1:
    throw "x should be 1"
"""

TOP_LEVEL_THROW = """\
throw "top level boom"

fn test_never_runs():
  if 1 == 1:
    print "x"
"""

TOP_LEVEL_RETURN = """\
return

fn test_never_runs():
  if 1 == 1:
    print "x"
"""

TYPE_ERROR = """\
fn test_bad_type():
  let x: int = "not an int"
"""

BOT_WITH_TEST = """\
bot "TestBot":
  description: "a bot with a test"

command ping:
  description: "Answer with pong"
  reply: "Pong!"

fn test_in_bot():
  if 1 == 1:
    print "x"
"""

MIXED = """\
fn add(a: int, b: int) -> int:
  return a + b

fn test_first_fn():
  if add(1, 2) != 3:
    throw "no"

test "block in the middle":
  requires add(2, 2) == 4

fn test_last_fn():
  if add(3, 3) != 6:
    throw "no"

test "final block":
  if add(4, 4) != 8:
    throw "no"
"""

BLOCK_FAILING = """\
test "fails loudly":
  if 1 + 1 != 3:
    throw "one plus one should be three"

test "passes after a failure":
  requires 2 * 3 == 6
"""

BLOCK_DUP_FN = """\
fn test_same():
  print "x"

test "test_same":
  print "y"
"""

BLOCK_DUP_BLOCK = """\
test "same name":
  print "x"

test "same name":
  print "y"
"""

BLOCK_RETURN_VALUE = """\
test "returns a value":
  return 1
"""

BOT_WITH_BLOCK = """\
bot "TestBot":
  description: "a bot with a block test"

command ping:
  description: "Answer with pong"
  reply: "Pong!"

test "block in a bot":
  if 1 == 1:
    print "x"
"""


def _run_cli(*argv, cwd=None):
    return subprocess.run(
        [sys.executable, str(LAUNCHER), *argv],
        capture_output=True, text=True, cwd=cwd)


def _program(source):
    return resolve_source(source, "<test>").program


class TestDiscovery(unittest.TestCase):

    def test_prefix_and_source_order(self):
        tests = discover_tests(_program(PASSING))
        self.assertEqual([t.name for t in tests],
                         ["test_add", "test_add_requires"])
        self.assertEqual([t.kind for t in tests], ["fn", "fn"])
        self.assertEqual([t.call_name for t in tests],
                         ["test_add", "test_add_requires"])

    def test_helper_fn_is_not_a_test(self):
        program = _program(PASSING)
        self.assertIn("helper", [fn.name for fn in program.functions])
        self.assertNotIn("helper",
                         [t.name for t in discover_tests(program)])

    def test_blocks_are_discovered_with_display_names(self):
        tests = discover_tests(_program(MIXED))
        blocks = [t for t in tests if t.kind == "block"]
        self.assertEqual([t.name for t in blocks],
                         ["block in the middle", "final block"])
        self.assertEqual([t.call_name for t in blocks],
                         ["_newr_test_0", "_newr_test_1"])

    def test_mixed_forms_run_in_source_order(self):
        tests = discover_tests(_program(MIXED))
        self.assertEqual(
            [t.name for t in tests],
            ["test_first_fn", "block in the middle", "test_last_fn",
             "final block"])
        self.assertEqual([t.kind for t in tests],
                         ["fn", "block", "fn", "block"])

    def test_imported_test_blocks_are_not_discovered(self):
        # Like test fns: a block in an imported module is not the
        # main file's test.
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d) / "lib.newr"
            lib.write_text("test \"in lib\":\n"
                           "  if 1 != 1:\n"
                           "    throw \"no\"\n", encoding="utf-8")
            main = Path(d) / "main.newr"
            main.write_text("import \"lib.newr\"\n\n"
                            "test \"in main\":\n"
                            "  if 1 == 1:\n"
                            "    print \"yes\"\n", encoding="utf-8")
            program = resolve_source(
                main.read_text(encoding="utf-8"), str(main)).program
            self.assertEqual([t.name for t in discover_tests(program)],
                             ["in main"])

    def test_no_tests_in_plain_program(self):
        self.assertEqual(discover_tests(_program(NO_TESTS)), [])

    def test_imported_test_fns_are_not_discovered(self):
        # Tests live in the file passed to `newr test`; a test fn in
        # an imported module is not the main file's test.
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d) / "lib.newr"
            lib.write_text("fn test_in_lib():\n"
                           "  if 1 != 1:\n"
                           "    throw \"no\"\n", encoding="utf-8")
            main = Path(d) / "main.newr"
            main.write_text("import \"lib.newr\"\n\n"
                            "fn test_in_main():\n"
                            "  if 1 == 1:\n"
                            "    print \"yes\"\n", encoding="utf-8")
            program = resolve_source(
                main.read_text(encoding="utf-8"), str(main)).program
            self.assertEqual([fn.name for fn in discover_tests(program)],
                             ["test_in_main"])

    def test_params_are_an_error(self):
        tests = discover_tests(_program(WITH_PARAMS))
        errors = validate_tests(tests)
        self.assertEqual(len(errors), 1)
        self.assertIn("test_with_param", errors[0])
        self.assertIn("take none", errors[0])

    def test_no_params_is_fine(self):
        self.assertEqual(validate_tests(discover_tests(_program(PASSING))),
                         [])


class TestBotDetection(unittest.TestCase):

    def test_plain_program_is_not_a_bot(self):
        self.assertFalse(is_bot_program(_program(PASSING)))

    def test_hello_bot_is_a_bot(self):
        source = (ROOT / "examples" / "hello_bot.newr").read_text(
            encoding="utf-8")
        self.assertTrue(is_bot_program(_program(source)))


class TestHarness(unittest.TestCase):

    def test_harness_appends_try_catch_per_test(self):
        program = _program(PASSING)
        tests = discover_tests(program)
        before = len(program.stmts)
        synthesize_harness(program, tests)
        # Two statements per test (the BEGIN print, then the Try),
        # appended after the program's own top-level statements.
        self.assertEqual(len(program.stmts), before + 2 * len(tests))
        tries = [s for s in program.stmts[before:]
                 if isinstance(s, A.Try)]
        self.assertEqual(len(tries), len(tests))
        for stmt in tries:
            self.assertEqual(len(stmt.catches), 1)
            self.assertEqual(stmt.catches[0][0], "err")

    def test_harness_markers_roundtrip(self):
        # The synthesized harness, emitted and run, prints the
        # markers in order (py target).
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.newr"
            path.write_text(PASSING, encoding="utf-8")
            result = run_test_file(path, "py")
            self.assertEqual(result.failed, 0)
            self.assertEqual(result.not_run, 0)
            self.assertEqual(result.passed, 2)
            self.assertEqual(
                [t.name for t in result.tests],
                ["test_add", "test_add_requires"])


class TestParseMarkers(unittest.TestCase):

    def _tests(self, *names):
        program = _program("\n".join(
            f"fn {name}():\n  print \"x\"\n" for name in names))
        return discover_tests(program)

    def test_pass_and_fail(self):
        tests = self._tests("test_a", "test_b")
        stdout = ("NEWR_TEST_BEGIN test_a\n"
                  "NEWR_TEST_PASS test_a\n"
                  "NEWR_TEST_BEGIN test_b\n"
                  "NEWR_TEST_FAIL test_b\n"
                  "boom\n")
        cases = parse_markers(stdout, tests)
        self.assertEqual([(c.name, c.status) for c in cases],
                         [("test_a", "pass"), ("test_b", "fail")])
        self.assertEqual(cases[1].message, "boom")

    def test_begin_without_pass_is_a_failure(self):
        tests = self._tests("test_a")
        cases = parse_markers("NEWR_TEST_BEGIN test_a\n", tests,
                              failure_detail="traceback here")
        self.assertEqual(cases[0].status, "fail")
        self.assertIn("did not finish", cases[0].message)
        self.assertIn("traceback here", cases[0].message)

    def test_no_begin_is_not_run(self):
        tests = self._tests("test_a", "test_b")
        cases = parse_markers("NEWR_TEST_BEGIN test_a\n"
                              "NEWR_TEST_PASS test_a\n", tests)
        self.assertEqual([(c.name, c.status) for c in cases],
                         [("test_a", "pass"), ("test_b", "not_run")])

    def test_fail_message_collects_following_lines(self):
        tests = self._tests("test_a")
        stdout = ("NEWR_TEST_BEGIN test_a\n"
                  "NEWR_TEST_FAIL test_a\n"
                  "line one\n"
                  "line two\n")
        cases = parse_markers(stdout, tests)
        self.assertEqual(cases[0].message, "line one\nline two")

    def test_block_names_with_spaces_roundtrip(self):
        program = _program("test \"adds two numbers\":\n"
                           "  print \"x\"\n")
        tests = discover_tests(program)
        self.assertEqual([t.name for t in tests],
                         ["adds two numbers"])
        stdout = ("NEWR_TEST_BEGIN adds two numbers\n"
                  "NEWR_TEST_PASS adds two numbers\n")
        cases = parse_markers(stdout, tests)
        self.assertEqual([(c.name, c.status) for c in cases],
                         [("adds two numbers", "pass")])


class TestRunTestFile(unittest.TestCase):

    def _run(self, source, target="py"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "t.newr"
        path.write_text(source, encoding="utf-8")
        return run_test_file(path, target)

    def test_all_pass(self):
        result = self._run(PASSING)
        self.assertEqual(exit_code([result]), 0)
        self.assertEqual(result.passed, 2)

    def test_one_failure_continues_to_the_next_test(self):
        result = self._run(FAILING)
        self.assertEqual(exit_code([result]), 4)
        self.assertEqual(result.passed, 1)
        self.assertEqual(result.failed, 1)
        bad = result.tests[0]
        self.assertEqual(bad.name, "test_bad")
        self.assertIn("one plus one should be three", bad.message)
        self.assertEqual(result.tests[1].status, "pass")

    def test_no_tests_passes(self):
        result = self._run(NO_TESTS)
        self.assertEqual(exit_code([result]), 0)
        self.assertEqual(result.skipped, "no tests")
        self.assertEqual(result.tests, [])

    def test_params_are_a_build_error(self):
        result = self._run(WITH_PARAMS)
        self.assertEqual(exit_code([result]), 4)
        self.assertIn("test_with_param", result.error)

    def test_type_error_is_a_build_error(self):
        result = self._run(TYPE_ERROR)
        self.assertEqual(exit_code([result]), 4)
        self.assertIn("E2001", result.error)

    def test_top_level_throw_means_no_test_ran(self):
        result = self._run(TOP_LEVEL_THROW)
        self.assertEqual(exit_code([result]), 4)
        self.assertTrue(result.run_error)
        self.assertIn("before running any test", result.run_error)
        self.assertEqual(result.tests[0].status, "not_run")

    def test_top_level_return_means_harness_never_reached(self):
        result = self._run(TOP_LEVEL_RETURN)
        self.assertEqual(exit_code([result]), 4)
        self.assertEqual(result.tests[0].status, "fail")
        self.assertIn("did not reach", result.tests[0].message)

    def test_bot_program_is_skipped_not_run(self):
        # A bot program WITH a test fn is skipped (it needs a live
        # token to run); a bot program without tests reports
        # "no tests" instead, which is the accurate report.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bot.newr"
            path.write_text(BOT_WITH_TEST, encoding="utf-8")
            result = run_test_file(path, "py")
        self.assertEqual(exit_code([result]), 0)
        self.assertIn("bot program", result.skipped)
        self.assertEqual(result.tests[0].status, "skipped")

    def test_mixed_forms_all_pass_in_source_order(self):
        result = self._run(MIXED)
        self.assertEqual(exit_code([result]), 0)
        self.assertEqual(result.passed, 4)
        self.assertEqual(
            [t.name for t in result.tests],
            ["test_first_fn", "block in the middle", "test_last_fn",
             "final block"])

    def test_block_failure_continues_to_the_next_test(self):
        result = self._run(BLOCK_FAILING)
        self.assertEqual(exit_code([result]), 4)
        self.assertEqual(result.passed, 1)
        self.assertEqual(result.failed, 1)
        bad = result.tests[0]
        self.assertEqual(bad.name, "fails loudly")
        self.assertIn("one plus one should be three", bad.message)
        self.assertEqual(result.tests[1].status, "pass")

    def test_block_name_colliding_with_fn_is_a_build_error(self):
        result = self._run(BLOCK_DUP_FN)
        self.assertEqual(exit_code([result]), 4)
        self.assertIn("duplicate test name", result.error)
        self.assertIn("test_same", result.error)

    def test_duplicate_block_names_are_a_build_error(self):
        result = self._run(BLOCK_DUP_BLOCK)
        self.assertEqual(exit_code([result]), 4)
        self.assertIn("duplicate test name", result.error)
        self.assertIn("same name", result.error)

    def test_block_returning_a_value_is_a_build_error(self):
        result = self._run(BLOCK_RETURN_VALUE)
        self.assertEqual(exit_code([result]), 4)
        self.assertIn("E2001", result.error)

    def test_bot_program_with_block_is_skipped_not_run(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bot.newr"
            path.write_text(BOT_WITH_BLOCK, encoding="utf-8")
            result = run_test_file(path, "py")
        self.assertEqual(exit_code([result]), 0)
        self.assertIn("bot program", result.skipped)
        self.assertEqual(result.tests[0].name, "block in a bot")
        self.assertEqual(result.tests[0].status, "skipped")

    def test_missing_file_is_an_environment_error(self):
        with self.assertRaises(TestEnvError):
            run_test_file(Path("/nonexistent/t.newr"), "py")


class TestCollectPaths(unittest.TestCase):

    def test_no_args_uses_current_directory(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "b.newr").write_text("print \"b\"\n",
                                            encoding="utf-8")
            (Path(d) / "a.newr").write_text("print \"a\"\n",
                                            encoding="utf-8")
            (Path(d) / "note.txt").write_text("x", encoding="utf-8")
            import os
            old = os.getcwd()
            try:
                os.chdir(d)
                self.assertEqual([p.name for p in collect_paths([])],
                                 ["a.newr", "b.newr"])
            finally:
                os.chdir(old)

    def test_directory_arg_is_recursive(self):
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d) / "sub"
            sub.mkdir()
            (sub / "deep.newr").write_text("print \"x\"\n",
                                            encoding="utf-8")
            (Path(d) / "top.newr").write_text("print \"x\"\n",
                                              encoding="utf-8")
            names = [p.name for p in collect_paths([d])]
            self.assertEqual(names, ["deep.newr", "top.newr"])

    def test_missing_path_is_an_environment_error(self):
        with self.assertRaises(TestEnvError):
            collect_paths(["/nonexistent/path.newr"])


class TestCli(unittest.TestCase):

    def _write(self, directory, source, name="t.newr"):
        path = Path(directory) / name
        path.write_text(source, encoding="utf-8")
        return path

    def test_all_pass_exit_0(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, PASSING)
            run = _run_cli("test", str(path))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("test_add ... ok", run.stdout)
            self.assertIn("2 passed", run.stdout)

    def test_failure_exit_4_names_the_test(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, FAILING)
            run = _run_cli("test", str(path))
            self.assertEqual(run.returncode, 4, run.stdout + run.stderr)
            self.assertIn("test_bad ... FAIL", run.stdout)
            self.assertIn("one plus one should be three", run.stdout)
            self.assertIn("test_good ... ok", run.stdout)

    def test_js_target(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, PASSING)
            run = _run_cli("test", "--target", "js", str(path))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("2 passed", run.stdout)

    def test_no_tests_exit_0(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, NO_TESTS)
            run = _run_cli("test", str(path))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("no tests", run.stdout)

    def test_bot_example_is_skipped(self):
        # hello_bot.newr itself has no test fns, so it reports
        # "no tests"; a bot file WITH a test fn is skipped.
        run = _run_cli("test", str(ROOT / "examples" / "hello_bot.newr"))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("no tests", run.stdout)
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, BOT_WITH_TEST, "bot.newr")
            run = _run_cli("test", str(path))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("bot program", run.stdout)

    def test_missing_file_exit_2(self):
        run = _run_cli("test", "/nonexistent/t.newr")
        self.assertEqual(run.returncode, 2)

    def test_unknown_flag_exit_1(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, PASSING)
            run = _run_cli("test", "--bogus", str(path))
            self.assertEqual(run.returncode, 1)

    def test_unknown_target_exit_1(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, PASSING)
            run = _run_cli("test", "--target", "go", str(path))
            self.assertEqual(run.returncode, 1)

    def test_build_error_exit_4(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, TYPE_ERROR)
            run = _run_cli("test", str(path))
            self.assertEqual(run.returncode, 4, run.stdout + run.stderr)
            self.assertIn("build error", run.stdout)

    def test_directory_arg_runs_every_file(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, PASSING, "a.newr")
            self._write(d, FAILING, "b.newr")
            run = _run_cli("test", d)
            self.assertEqual(run.returncode, 4, run.stdout + run.stderr)
            self.assertIn("a.newr", run.stdout)
            self.assertIn("b.newr", run.stdout)
            self.assertIn("test_bad ... FAIL", run.stdout)

    def test_no_args_runs_current_directory(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, PASSING)
            run = _run_cli("test", cwd=d)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("2 passed", run.stdout)

    def test_help(self):
        run = _run_cli("help", "test")
        self.assertEqual(run.returncode, 0)
        self.assertIn("fn test_", run.stdout)
        self.assertIn('test "display name"', run.stdout)
        self.assertIn("Usage: newr test", run.stdout)

    def test_mixed_forms_exit_0(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, MIXED)
            run = _run_cli("test", str(path))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("block in the middle ... ok", run.stdout)
            self.assertIn("final block ... ok", run.stdout)
            self.assertIn("4 passed", run.stdout)

    def test_block_failure_exit_4(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, BLOCK_FAILING)
            run = _run_cli("test", str(path))
            self.assertEqual(run.returncode, 4, run.stdout + run.stderr)
            self.assertIn("fails loudly ... FAIL", run.stdout)
            self.assertIn("one plus one should be three", run.stdout)
            self.assertIn("passes after a failure ... ok", run.stdout)

    def test_duplicate_test_name_exit_4(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, BLOCK_DUP_FN)
            run = _run_cli("test", str(path))
            self.assertEqual(run.returncode, 4, run.stdout + run.stderr)
            self.assertIn("build error", run.stdout)
            self.assertIn("duplicate test name", run.stdout)

    def test_json_envelope_pass(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, PASSING)
            run = _run_cli("--json", "test", str(path))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            envelope = json.loads(run.stdout)
            self.assertTrue(envelope["ok"])
            self.assertEqual(envelope["command"], "test")
            self.assertEqual(envelope["data"]["passed"], 2)
            self.assertEqual(envelope["data"]["failed"], 0)

    def test_json_envelope_fail(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, FAILING)
            run = _run_cli("--json", "test", str(path))
            self.assertEqual(run.returncode, 4, run.stdout + run.stderr)
            envelope = json.loads(run.stdout)
            self.assertFalse(envelope["ok"])
            self.assertEqual(envelope["data"]["failed"], 1)
            self.assertEqual(envelope["error"]["code"], "TEST_FAILED")

    def test_manifest_target_is_used(self):
        # A manifest with target js makes `newr test` run js
        # without --target (same rule as `newr build`).
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "newr.json").write_text(
                json.dumps({"name": "demo", "version": "0.1.0",
                            "target": "js"}), encoding="utf-8")
            path = self._write(d, PASSING)
            run = _run_cli("test", str(path))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn("2 passed", run.stdout)


class TestReporting(unittest.TestCase):

    def test_format_human_and_json_data(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.newr"
            path.write_text(FAILING, encoding="utf-8")
            result = run_test_file(path, "py")
        text = format_human([result])
        self.assertIn("test_bad ... FAIL", text)
        self.assertIn("1 passed, 1 failed", text)
        data = to_json_data([result])
        self.assertEqual(data["passed"], 1)
        self.assertEqual(data["failed"], 1)
        self.assertEqual(data["files"][0]["file"], str(path))
        self.assertEqual(exit_code([result]), 4)


if __name__ == "__main__":
    unittest.main()
