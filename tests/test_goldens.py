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

from conformance.run import (GOLDENS, GOLDENS_JS, GOLDEN_DIR,
                             GOLDEN_DIR_JS, ROOT, _build_to_temp)


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
