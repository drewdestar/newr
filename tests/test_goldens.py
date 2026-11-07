"""Golden-file tests (BUILD_REPORT.md D3).

Compare-only: each test builds a golden's source (from examples/)
to a temp dir and diffs the emitted text against goldens/py/*.py.
A mismatch fails — the golden is the contract. Regeneration needs
an explicit `python3 conformance/run.py --bless [--only <name>]`;
never hand-edit a golden.
"""

import difflib
import unittest
from pathlib import Path

from conformance.run import (GOLDENS, GOLDENS_JS, GOLDENS_JAVA,
                             GOLDENS_CS, GOLDENS_RS, GOLDENS_KT,
                             GOLDENS_DART, GOLDEN_DIR, GOLDEN_DIR_JS,
                             GOLDEN_DIR_JAVA, GOLDEN_DIR_CS,
                             GOLDEN_DIR_RS, GOLDEN_DIR_KT,
                             GOLDEN_DIR_DART, ROOT,
                             _build_to_temp)


class TestGoldens(unittest.TestCase):
    def test_goldens_match_emitted_output(self):
        for name in sorted(GOLDENS):
            with self.subTest(golden=name):
                source, outputs = GOLDENS[name]
                built = _build_to_temp(Path(source))
                for out in outputs:
                    golden = GOLDEN_DIR / out
                    self.assertTrue(golden.is_file(),
                                    f"golden {golden} is missing — "
                                    f"run `python3 conformance/run.py "
                                    f"--bless --only {name}`")
                    expected = golden.read_text(encoding="utf-8")
                    actual = built[out]
                    if actual != expected:
                        diff = "".join(difflib.unified_diff(
                            expected.splitlines(keepends=True),
                            actual.splitlines(keepends=True),
                            fromfile=f"golden {out}",
                            tofile=f"emitted {out}"))
                        self.fail(f"golden {out} is stale:\n{diff}")


class TestGoldensJs(unittest.TestCase):
    """The JS twins (M3): same sources, `.js` outputs, compare-only.

    Regeneration needs an explicit
    `python3 conformance/run.py --bless --target js [--only <name>]`;
    never hand-edit a golden.
    """

    def test_goldens_js_match_emitted_output(self):
        for name in sorted(GOLDENS_JS):
            with self.subTest(golden=name):
                source, outputs = GOLDENS_JS[name]
                built = _build_to_temp(Path(source), target="js")
                for out in outputs:
                    golden = GOLDEN_DIR_JS / out
                    self.assertTrue(golden.is_file(),
                                    f"golden {golden} is missing — "
                                    f"run `python3 conformance/run.py "
                                    f"--bless --target js --only {name}`")
                    expected = golden.read_text(encoding="utf-8")
                    actual = built[out]
                    if actual != expected:
                        diff = "".join(difflib.unified_diff(
                            expected.splitlines(keepends=True),
                            actual.splitlines(keepends=True),
                            fromfile=f"golden {out}",
                            tofile=f"emitted {out}"))
                        self.fail(f"golden {out} is stale:\n{diff}")

    def test_showcase_golden_is_v0_golden(self):
        # D2: showcase.newr is byte-identical to the v0 golden
        # examples/showcase.py (the v0 file is CI-pinned by SHA-256).
        v0 = (ROOT / "examples" / "showcase.py").read_text(encoding="utf-8")
        golden = (GOLDEN_DIR / "showcase.py").read_text(encoding="utf-8")
        self.assertEqual(golden, v0)

    def test_hello_bot_golden_differs_from_v0_in_header_only(self):
        # D2: the v0 golden examples/hello.py came from a source named
        # hello.newr; the M2 golden comes from hello_bot.newr. The two
        # files differ in exactly the 2 header lines that name the
        # source file.
        v0 = (ROOT / "examples" / "hello.py").read_text(encoding="utf-8")
        golden = (GOLDEN_DIR / "hello_bot.py").read_text(encoding="utf-8")
        v0_lines = v0.splitlines()
        g_lines = golden.splitlines()
        self.assertEqual(len(v0_lines), len(g_lines))
        diffs = [i for i, (a, b) in enumerate(zip(v0_lines, g_lines))
                 if a != b]
        self.assertEqual(len(diffs), 2, diffs)
        for i in diffs:
            self.assertIn("hello", v0_lines[i])
            self.assertIn("hello_bot", g_lines[i])


class TestGoldensJava(unittest.TestCase):
    """The Java twins (M4): same sources, `.java` outputs,
    compare-only. Regeneration needs an explicit
    `python3 conformance/run.py --bless --target java [--only <name>]`;
    never hand-edit a golden.
    """

    def test_goldens_java_match_emitted_output(self):
        for name in sorted(GOLDENS_JAVA):
            with self.subTest(golden=name):
                source, outputs = GOLDENS_JAVA[name]
                built = _build_to_temp(Path(source), target="java")
                for out in outputs:
                    golden = GOLDEN_DIR_JAVA / out
                    self.assertTrue(golden.is_file(),
                                    f"golden {golden} is missing — "
                                    f"run `python3 conformance/run.py "
                                    f"--bless --target java --only {name}`")
                    expected = golden.read_text(encoding="utf-8")
                    actual = built[out]
                    if actual != expected:
                        diff = "".join(difflib.unified_diff(
                            expected.splitlines(keepends=True),
                            actual.splitlines(keepends=True),
                            fromfile=f"golden {out}",
                            tofile=f"emitted {out}"))
                        self.fail(f"golden {out} is stale:\n{diff}")


class TestGoldensCs(unittest.TestCase):
    """The C# twins (M4): same sources, `.cs` outputs,
    compare-only. Regeneration needs an explicit
    `python3 conformance/run.py --bless --target cs [--only <name>]`;
    never hand-edit a golden.
    """

    def test_goldens_cs_match_emitted_output(self):
        for name in sorted(GOLDENS_CS):
            with self.subTest(golden=name):
                source, outputs = GOLDENS_CS[name]
                built = _build_to_temp(Path(source), target="cs")
                for out in outputs:
                    golden = GOLDEN_DIR_CS / out
                    self.assertTrue(golden.is_file(),
                                    f"golden {golden} is missing — "
                                    f"run `python3 conformance/run.py "
                                    f"--bless --target cs --only {name}`")
                    expected = golden.read_text(encoding="utf-8")
                    actual = built[out]
                    if actual != expected:
                        diff = "".join(difflib.unified_diff(
                            expected.splitlines(keepends=True),
                            actual.splitlines(keepends=True),
                            fromfile=f"golden {out}",
                            tofile=f"emitted {out}"))
                        self.fail(f"golden {out} is stale:\n{diff}")


class TestGoldensRs(unittest.TestCase):
    """The Rust twins (M5): same sources, `.rs` outputs,
    compare-only. Regeneration needs an explicit
    `python3 conformance/run.py --bless --target rs [--only <name>]`;
    never hand-edit a golden.

    M5 note: the "shop" entry does NOT list `shop_items.rs`. The
    module copy emitted as part of shop.newr differs from the
    standalone copy on purpose (module form has `pub fn
    __newr_init`, standalone form has `fn main`), so the two
    cannot share one golden; the standalone form is pinned by the
    "shop_items" entry and the module form is exercised at runtime
    by the shop conformance program.
    """

    def test_goldens_rs_match_emitted_output(self):
        for name in sorted(GOLDENS_RS):
            with self.subTest(golden=name):
                source, outputs = GOLDENS_RS[name]
                built = _build_to_temp(Path(source), target="rs")
                for out in outputs:
                    golden = GOLDEN_DIR_RS / out
                    self.assertTrue(golden.is_file(),
                                    f"golden {golden} is missing — "
                                    f"run `python3 conformance/run.py "
                                    f"--bless --target rs --only {name}`")
                    expected = golden.read_text(encoding="utf-8")
                    actual = built[out]
                    if actual != expected:
                        diff = "".join(difflib.unified_diff(
                            expected.splitlines(keepends=True),
                            actual.splitlines(keepends=True),
                            fromfile=f"golden {out}",
                            tofile=f"emitted {out}"))
                        self.fail(f"golden {out} is stale:\n{diff}")


class TestGoldensKt(unittest.TestCase):
    """The Kotlin twins (M5): same sources, `.kt` outputs,
    compare-only. Regeneration needs an explicit
    `python3 conformance/run.py --bless --target kt [--only <name>]`;
    never hand-edit a golden.

    M5 note: the "shop" entry does NOT list `shop_items.kt`. The
    module copy emitted as part of shop.newr differs from the
    standalone copy on purpose (module form has no top-level
    `main`, standalone form has a `fun main` wrapper), so the two
    cannot share one golden; the standalone form is pinned by the
    "shop_items" entry and the module form is exercised at runtime
    by the shop conformance program.
    """

    def test_goldens_kt_match_emitted_output(self):
        for name in sorted(GOLDENS_KT):
            with self.subTest(golden=name):
                source, outputs = GOLDENS_KT[name]
                built = _build_to_temp(Path(source), target="kt")
                for out in outputs:
                    golden = GOLDEN_DIR_KT / out
                    self.assertTrue(golden.is_file(),
                                    f"golden {golden} is missing — "
                                    f"run `python3 conformance/run.py "
                                    f"--bless --target kt --only {name}`")
                    expected = golden.read_text(encoding="utf-8")
                    actual = built[out]
                    if actual != expected:
                        diff = "".join(difflib.unified_diff(
                            expected.splitlines(keepends=True),
                            actual.splitlines(keepends=True),
                            fromfile=f"golden {out}",
                            tofile=f"emitted {out}"))
                        self.fail(f"golden {out} is stale:\n{diff}")


class TestGoldensDart(unittest.TestCase):
    """The Dart twins (M5): same sources, `.dart` outputs,
    compare-only. Regeneration needs an explicit
    `python3 conformance/run.py --bless --target dart [--only <name>]`;
    never hand-edit a golden.
    """

    def test_goldens_dart_match_emitted_output(self):
        for name in sorted(GOLDENS_DART):
            with self.subTest(golden=name):
                source, outputs = GOLDENS_DART[name]
                built = _build_to_temp(Path(source), target="dart")
                for out in outputs:
                    golden = GOLDEN_DIR_DART / out
                    self.assertTrue(golden.is_file(),
                                    f"golden {golden} is missing — "
                                    f"run `python3 conformance/run.py "
                                    f"--bless --target dart --only {name}`")
                    expected = golden.read_text(encoding="utf-8")
                    actual = built[out]
                    if actual != expected:
                        diff = "".join(difflib.unified_diff(
                            expected.splitlines(keepends=True),
                            actual.splitlines(keepends=True),
                            fromfile=f"golden {out}",
                            tofile=f"emitted {out}"))
                        self.fail(f"golden {out} is stale:\n{diff}")
