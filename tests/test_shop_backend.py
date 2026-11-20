"""Tests for the shop backend (examples/shop_backend.newr, M8d
Phase 3).

Builds the backend for py in a scratch dir (fresh shop.db), starts
it, waits for GET /products to answer, then drives the whole cart
flow over urllib: list products, add Tea, add Tea again, read the
cart, check out, read the empty cart. The server is killed in a
finally block. Temp dirs live under SCRATCH_ROOT (~/workspace,
never /tmp: it is a 512 MB tmpfs), and every timeout is generous:
this VM has a small CPU and several agents run in parallel.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "newr"

from compiler.app_pack import SCRATCH_ROOT

# Generous timeouts: the VM CPU is small and several agents run in
# parallel, so a build or a server start must never fail on a tight
# clock.
BUILD_TIMEOUT = 300
START_TIMEOUT = 60
REQ_TIMEOUT = 30


def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(LAUNCHER), *args],
                          capture_output=True, text=True, cwd=cwd,
                          timeout=BUILD_TIMEOUT)


def temp_dir():
    """A temp dir under ~/workspace (SCRATCH_ROOT), never /tmp."""
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(SCRATCH_ROOT))


def http_get(url):
    """GET url, return (status, parsed JSON body)."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def http_post(url, payload):
    """POST a JSON payload, return (status, parsed JSON body)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


class TestShopBackend(unittest.TestCase):
    def test_full_cart_flow(self):
        with temp_dir() as d:
            root = Path(d)
            for name in ("shop_shared.newr", "shop_backend.newr"):
                shutil.copyfile(ROOT / "examples" / name,
                                root / name)
            r = run_cli("build", "shop_backend.newr",
                        "--target", "py", cwd=root)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((root / "shop_backend.py").is_file())
            proc = subprocess.Popen(
                [sys.executable, "shop_backend.py"],
                cwd=root, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True)
            try:
                base = "http://127.0.0.1:8080"
                deadline = time.time() + START_TIMEOUT
                products = None
                while time.time() < deadline:
                    if proc.poll() is not None:
                        self.fail("backend exited early: "
                                  + proc.stdout.read())
                    try:
                        _, products = http_get(base + "/products")
                        break
                    except (urllib.error.URLError,
                            ConnectionError, TimeoutError):
                        time.sleep(0.5)
                self.assertIsNotNone(
                    products, "backend never answered /products")
                self.assertEqual(
                    [(p["id"], p["name"], p["price"]) for p in products],
                    [(1, "Tea", 5000), (2, "Coffee", 7000),
                     (3, "Cake", 12000)])
                self.assertTrue(all(
                    p["image_url"].startswith("https://example.com/")
                    and p["image_url"].endswith(".png")
                    for p in products))

                status, cart = http_post(
                    base + "/cart", {"product_id": 1, "qty": 1})
                self.assertEqual(status, 200)
                self.assertEqual(
                    cart, [{"product_id": 1, "qty": 1, "price": 5000}])

                status, cart = http_post(
                    base + "/cart", {"product_id": 1, "qty": 1})
                self.assertEqual(status, 200)
                self.assertEqual(
                    cart, [{"product_id": 1, "qty": 2, "price": 5000}])

                status, cart = http_get(base + "/cart")
                self.assertEqual(status, 200)
                self.assertEqual(
                    cart, [{"product_id": 1, "qty": 2, "price": 5000}])

                status, cart = http_post(base + "/checkout", {})
                self.assertEqual(status, 200)
                self.assertEqual(cart, [])

                status, cart = http_get(base + "/cart")
                self.assertEqual(status, 200)
                self.assertEqual(cart, [])
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
