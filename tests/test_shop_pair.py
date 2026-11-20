"""Tests for the shop pair (M8d Phase 4).

Starts the py backend (port 8080) and the js frontend (port
8081) from fresh scratch dirs, builds both with ./newr build,
then verifies the pair over HTTP with urllib: GET / serves the
page with Tea, Coffee, Cake, Buy buttons data-action 0, 1, 2,
Checkout data-action 3, and the search input data-input 0;
GET /action?i=0 returns 302 to / and the backend cart then
shows one Tea line; GET /?q=Coffee shows Coffee only;
GET /action?i=3 returns 302 and the backend cart is then empty.
Both servers are killed in a finally block. Temp dirs live
under SCRATCH_ROOT (~/workspace, never /tmp: it is a 512 MB
tmpfs), and every timeout is generous: this VM has a small CPU
and several agents run in parallel.
"""

import json
import re
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

BACKEND = "http://127.0.0.1:8080"
FRONTEND = "http://127.0.0.1:8081"


def run_cli(*args, cwd=None):
    return subprocess.run([sys.executable, str(LAUNCHER), *args],
                          capture_output=True, text=True, cwd=cwd,
                          timeout=BUILD_TIMEOUT)


def temp_dir():
    """A temp dir under ~/workspace (SCRATCH_ROOT), never /tmp."""
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(SCRATCH_ROOT))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """An opener that does not follow redirects: urlopen raises
    HTTPError with the 302 status and headers instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


NO_REDIRECT = urllib.request.build_opener(NoRedirect)


def http_get_text(url):
    """GET url, return (status, body text). Follows redirects."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as resp:
        return resp.status, resp.read().decode("utf-8")


def http_get_json(url):
    """GET url, return (status, parsed JSON body)."""
    status, body = http_get_text(url)
    return status, json.loads(body)


def http_get_no_redirect(url):
    """GET url without following redirects. Returns (status,
    headers dict, body text); a 302 arrives as HTTPError and is
    turned into its status and headers here."""
    req = urllib.request.Request(url, method="GET")
    try:
        with NO_REDIRECT.open(req, timeout=REQ_TIMEOUT) as resp:
            return resp.status, dict(resp.headers), \
                resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), \
            err.read().decode("utf-8")


def wait_for(url, proc, proc_name, timeout=START_TIMEOUT):
    """Poll GET url until it answers 200, or fail. The proc must
    stay alive while waiting; an early exit fails with its log."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            self_fail = f"{proc_name} exited early: {out}"
            raise AssertionError(self_fail)
        try:
            status, _ = http_get_text(url)
            if status == 200:
                return
        except (urllib.error.URLError, ConnectionError,
                TimeoutError):
            time.sleep(0.5)
    raise AssertionError(f"{proc_name} never answered {url}")


def stop(proc):
    """Terminate a server proc, escalating to kill on timeout."""
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    if proc.stdout is not None:
        proc.stdout.close()


class TestShopPair(unittest.TestCase):
    def test_pair_buy_and_checkout(self):
        with temp_dir() as backend_dir, temp_dir() as frontend_dir:
            back = Path(backend_dir)
            front = Path(frontend_dir)
            for name in ("shop_shared.newr", "shop_backend.newr"):
                shutil.copyfile(ROOT / "examples" / name,
                                back / name)
            for name in ("shop_shared.newr", "shop_view.newr",
                         "shop_frontend_js.newr"):
                shutil.copyfile(ROOT / "examples" / name,
                                front / name)
            r = run_cli("build", "shop_backend.newr",
                        "--target", "py", cwd=back)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue((back / "shop_backend.py").is_file())
            r = run_cli("build", "shop_frontend_js.newr",
                        "--target", "js", cwd=front)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(
                (front / "shop_frontend_js.js").is_file())

            backend_proc = subprocess.Popen(
                [sys.executable, "shop_backend.py"],
                cwd=back, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True)
            try:
                wait_for(BACKEND + "/products", backend_proc,
                         "backend")
                frontend_proc = subprocess.Popen(
                    ["node", "shop_frontend_js.js"],
                    cwd=front, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True)
                try:
                    wait_for(FRONTEND + "/", frontend_proc,
                             "frontend")

                    # The page lists all three products, the Buy
                    # buttons in pinned order, Checkout last, and
                    # the search input.
                    status, page = http_get_text(FRONTEND + "/")
                    self.assertEqual(status, 200)
                    for name in ("Tea", "Coffee", "Cake"):
                        self.assertIn(name, page)
                    actions = re.findall(
                        r'data-action="(\d+)"', page)
                    self.assertEqual(actions, ["0", "1", "2", "3"])
                    inputs = re.findall(
                        r'data-input="(\d+)"', page)
                    self.assertEqual(inputs, ["0"])
                    self.assertEqual(
                        page.count(">Buy</button>"), 3)
                    self.assertIn(">Checkout</button>", page)

                    # Buy Tea: /action?i=0 redirects to /, and the
                    # backend cart then holds one Tea line.
                    status, headers, _ = http_get_no_redirect(
                        FRONTEND + "/action?i=0")
                    self.assertEqual(status, 302)
                    self.assertEqual(headers.get("Location"), "/")
                    status, cart = http_get_json(BACKEND + "/cart")
                    self.assertEqual(status, 200)
                    self.assertEqual(
                        cart,
                        [{"product_id": 1, "qty": 1,
                          "price": 5000}])

                    # Search filters to Coffee only.
                    status, page = http_get_text(
                        FRONTEND + "/?q=Coffee")
                    self.assertEqual(status, 200)
                    self.assertIn("Coffee", page)
                    self.assertNotIn("Tea", page)
                    self.assertNotIn("Cake", page)

                    # Checkout: /action?i=3 redirects to /, and
                    # the backend cart is then empty.
                    status, headers, _ = http_get_no_redirect(
                        FRONTEND + "/action?i=3")
                    self.assertEqual(status, 302)
                    self.assertEqual(headers.get("Location"), "/")
                    status, cart = http_get_json(BACKEND + "/cart")
                    self.assertEqual(status, 200)
                    self.assertEqual(cart, [])
                finally:
                    stop(frontend_proc)
            finally:
                stop(backend_proc)


if __name__ == "__main__":
    unittest.main()
