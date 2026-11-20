"""Tests for the shop view button order (examples/shop_view.newr,
M8d Phase 3).

Builds a small probe program for py that renders the shop view with
the three seeded products and prints the HTML, then asserts the
pinned order: Buy buttons in product order (data-action 0, 1, 2),
Checkout last (data-action 3), the search input is data-input 0,
and all three product names appear. The order matters because the
js frontend dispatches /action by button index (SPEC 22.3). Temp
dirs live under SCRATCH_ROOT (~/workspace, never /tmp: it is a
512 MB tmpfs), and every timeout is generous: this VM has a small
CPU and several agents run in parallel.
"""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"

from compiler.app_pack import SCRATCH_ROOT

BUILD_TIMEOUT = 300
RUN_TIMEOUT = 120

PROBE = (
    'import "shop_shared.newr"\n'
    'import "shop_view.newr"\n'
    '\n'
    'pub fn buy(id: int) -> none:\n'
    '  pass\n'
    '\n'
    'pub fn checkout() -> none:\n'
    '  pass\n'
    '\n'
    'let products: list[Product] = [shop_shared.Product(id: 1, name: "Tea", price: 5000, image_url: "https://example.com/tea.png"), shop_shared.Product(id: 2, name: "Coffee", price: 7000, image_url: "https://example.com/coffee.png"), shop_shared.Product(id: 3, name: "Cake", price: 12000, image_url: "https://example.com/cake.png")]\n'
    'print ui.render(shop_view.shop_view(products, 0, "", buy, checkout))\n'
)


def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(LAUNCHER), *args],
                          capture_output=True, text=True, cwd=cwd,
                          timeout=BUILD_TIMEOUT)


def temp_dir():
    """A temp dir under ~/workspace (SCRATCH_ROOT), never /tmp."""
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(SCRATCH_ROOT))


def render_html():
    """Build the probe for py, run it, return the rendered HTML."""
    with temp_dir() as d:
        root = Path(d)
        for name in ("shop_shared.newr", "shop_view.newr"):
            shutil.copyfile(ROOT / "examples" / name, root / name)
        (root / "probe.newr").write_text(PROBE, encoding="utf-8")
        r = run_cli("build", "probe.newr", "--target", "py", cwd=root)
        if r.returncode != 0:
            raise AssertionError(
                "probe build failed: " + r.stdout + r.stderr)
        run = subprocess.run(
            [sys.executable, "probe.py"], capture_output=True,
            text=True, cwd=root, timeout=RUN_TIMEOUT)
        if run.returncode != 0:
            raise AssertionError(
                "probe run failed: " + run.stdout + run.stderr)
        return run.stdout.strip()


class TestShopViewOrder(unittest.TestCase):
    def test_button_order_matches_dispatch_table(self):
        html = render_html()
        actions = re.findall(r'data-action="(\d+)"', html)
        self.assertEqual(actions, ["0", "1", "2", "3"])
        inputs = re.findall(r'data-input="(\d+)"', html)
        self.assertEqual(inputs, ["0"])
        for name in ("Tea", "Coffee", "Cake"):
            self.assertIn(name, html)
        self.assertLess(html.index("Tea"), html.index("Coffee"))
        self.assertLess(html.index("Coffee"), html.index("Cake"))
        self.assertEqual(html.count(">Buy</button>"), 3)
        self.assertIn(">Checkout</button>", html)
        self.assertLess(html.index(">Buy</button>"),
                        html.index(">Checkout</button>"))
        self.assertLess(html.rindex(">Buy</button>"),
                        html.index(">Checkout</button>"))


if __name__ == "__main__":
    unittest.main()
