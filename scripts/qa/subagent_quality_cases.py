"""Prepare and check frozen subagent implementation acceptance cases.

Use ``python scripts/qa/subagent_quality_cases.py prepare CASE_DIR`` to create a
new fixture, adding ``--scenario followup`` for the controlled continuation case.
After an independently run implementation attempt, use ``check CASE_DIR`` to run
the frozen acceptance checks against its parent workspace. This script never
launches Alysis or calls a model provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

FILES = {
    "orderquote/__init__.py": "",
    "orderquote/settings.py": 'CURRENCY = "USD"\n',
    "orderquote/catalog.py": 'PRICES = {"TEA": "3.35", "COFFEE": "7.10", "MUG": "12.00", "PIN": "0.05"}\n',
    "orderquote/engine.py": """from .catalog import PRICES
from .settings import CURRENCY


def quote(items, coupon=None):
    total = sum(float(PRICES[item["sku"]]) * item["quantity"] for item in items)
    return {"currency": CURRENCY, "items": items, "total": total}
""",
    "orderquote/__main__.py": """import argparse
import json
from .engine import quote


def main():
    parser = argparse.ArgumentParser(description="Quote a cart")
    parser.add_argument("items", nargs="+")
    args = parser.parse_args()
    items = []
    for token in args.items:
        sku, quantity = token.split(":")
        items.append({"sku": sku, "quantity": int(quantity)})
    print(json.dumps(quote(items)))


if __name__ == "__main__":
    main()
""",
    "tests/test_smoke.py": """import unittest
from orderquote.engine import quote
from orderquote.settings import CURRENCY


class QuoteSmoke(unittest.TestCase):
    def test_preserves_shop_currency(self):
        self.assertEqual(quote([{"sku": "TEA", "quantity": 2}])["currency"], CURRENCY)
""",
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n",
    "README.md": """# Orderquote

A small dependency-free Python cart quote library and command line application.
Public entry points: orderquote.engine.quote(items, coupon=None) and
python -m orderquote SKU:quantity [SKU:quantity ...]. Catalog prices are decimal
strings. Shop configuration lives in orderquote/settings.py.

Run local checks with python -m unittest discover -s tests -v.
""",
}

REQUIREMENTS = """Improve this small cart quote library and CLI and run relevant checks.
The public API is orderquote.engine.quote(items, coupon=None), where items is a
nonempty list of {sku, quantity} dictionaries. Normalize SKU whitespace/case,
merge repeated SKUs, and return items sorted by SKU with sku, quantity, line_total.
Reject unknown SKUs, an empty cart, and quantities that are not positive integers
(booleans and fractional numbers are invalid). Raise ValueError for invalid input.
Return currency, items, subtotal, discount, shipping, total. Every money field,
including line_total, must be a string with two decimals. Use decimal arithmetic.
The optional SAVE10 coupon (trimmed, case insensitive) discounts the whole subtotal
by 10 percent, rounded to cents with half-up rounding. Reject other nonempty
coupons. Shipping is 4.50 unless subtotal minus rounded discount is at least 50.00,
when it is free. Total is subtotal minus discount plus shipping.
Extend python -m orderquote SKU:quantity ... with --coupon, emitting this JSON
shape. Invalid input must exit 2 with a helpful stderr message and no traceback.
Keep the existing uncommitted currency setting exactly as it is. Add useful tests
and summarize what changed and what you verified. Do not commit.
"""

FOLLOWUP = (
    """Use an available coding subagent in an isolated worktree to implement the
requirements below. Wait for its successful completion and review its result.
Then use subagent_resume with that completed child's run ID and an explicit new
task: add pickup=True to quote and --pickup to the CLI; pickup waives shipping
even below the free-shipping threshold, while normal delivery stays unchanged.
Continue the same retained candidate and conversation. Verify the final behavior
and explicitly apply the final candidate to the parent workspace. Do not apply an
earlier revision or start a fresh child for the follow-up. If a child actually
stops incomplete, recover it honestly before treating the first phase as complete.
This is a controlled follow-up test; the initial implementation requirements are:

"""
    + REQUIREMENTS
)

ACCEPTANCE = r"""import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(os.environ["ORDERQUOTE_CASE_ROOT"])
sys.path.insert(0, str(ROOT))
engine = importlib.import_module("orderquote.engine")


class Contract(unittest.TestCase):
    def quote(self, items, **kwargs):
        return engine.quote([{"sku": sku, "quantity": qty} for sku, qty in items], **kwargs)

    def cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        return subprocess.run([sys.executable, "-m", "orderquote", *args], cwd=ROOT,
                              env=env, capture_output=True, text=True, timeout=15)

    def test_merged_sorted_items_and_currency(self):
        self.assertEqual(self.quote([(" tea ", 1), ("MUG", 1), ("Tea", 2)]), {
            "currency": "EUR", "items": [
                {"sku": "MUG", "quantity": 1, "line_total": "12.00"},
                {"sku": "TEA", "quantity": 3, "line_total": "10.05"}],
            "subtotal": "22.05", "discount": "0.00", "shipping": "4.50", "total": "26.55"})

    def test_fractional_cent_discount_rounds_half_up(self):
        result = self.quote([("PIN", 1)], coupon=" save10 ")
        self.assertEqual((result["subtotal"], result["discount"], result["shipping"], result["total"]),
                         ("0.05", "0.01", "4.50", "4.54"))

    def test_shipping_uses_discounted_amount(self):
        result = self.quote([("MUG", 4), ("TEA", 1)], coupon="SAVE10")
        self.assertEqual((result["subtotal"], result["discount"], result["shipping"], result["total"]),
                         ("51.35", "5.14", "4.50", "50.71"))

    def test_shipping_threshold_exact_and_below(self):
        self.assertEqual(self.quote([("PIN", 1000)])["shipping"], "0.00")
        self.assertEqual(self.quote([("PIN", 999)])["shipping"], "4.50")
        self.assertEqual(self.quote([("MUG", 5)], coupon="SAVE10")["total"], "54.00")

    def test_invalid_cart_and_quantity(self):
        for items in ([], [("NOPE", 1)], [("TEA", 0)], [("TEA", -1)],
                      [("TEA", True)], [("TEA", 1.5)], [("TEA", "2")]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                self.quote(items)

    def test_invalid_coupon(self):
        with self.assertRaises(ValueError):
            self.quote([("TEA", 1)], coupon="SAVE99")

    def test_cli_matches_api(self):
        result = self.cli("tea:1", "TEA:2", "--coupon", "SAVE10")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.quote([("TEA", 3)], coupon="SAVE10"))

    def test_invalid_cli_has_no_traceback(self):
        for args in (("TEA:0",), ("NOPE:1",), ("TEA:1.5",), ("TEA",),
                     ("TEA:1", "--coupon", "NOPE")):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertTrue(result.stderr.strip())
                self.assertNotIn("Traceback", result.stderr)

    def test_original_dirty_setting_preserved(self):
        self.assertEqual((ROOT / "orderquote/settings.py").read_text(), 'CURRENCY = "EUR"\n')

    @unittest.skipUnless(os.environ["ORDERQUOTE_CASE_SCENARIO"] == "followup", "follow-up only")
    def test_pickup_followup(self):
        pickup = self.quote([("TEA", 1)], pickup=True)
        self.assertEqual((pickup["shipping"], pickup["total"]), ("0.00", "3.35"))
        self.assertEqual(self.quote([("TEA", 1)])["total"], "7.85")
        result = self.cli("TEA:1", "--pickup", "--coupon", "SAVE10")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.quote([("TEA", 1)], pickup=True, coupon="SAVE10"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
"""


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def prepare(destination: Path, scenario: str) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    workspace = destination / "workspace"
    workspace.mkdir()
    for relative, content in FILES.items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(workspace, "init", "-q")
    git(workspace, "add", ".")
    git(
        workspace,
        "-c",
        "user.name=Evaluation Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--no-verify",
        "-qm",
        "Frozen evaluation fixture",
    )
    (workspace / "orderquote/settings.py").write_text('CURRENCY = "EUR"\n')
    prompt = FOLLOWUP if scenario == "followup" else REQUIREMENTS
    (destination / "prompt.txt").write_text(prompt)
    (destination / "chat-input.txt").write_text(prompt.replace("\n", " ") + "\n/exit\n")
    (destination / "acceptance.py").write_text(ACCEPTANCE)
    manifest = {
        "scenario": scenario,
        "workspace": str(workspace),
        "baseline_head": git(workspace, "rev-parse", "HEAD"),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "acceptance_sha256": hashlib.sha256(ACCEPTANCE.encode()).hexdigest(),
        "dirty_paths": ["orderquote/settings.py"],
        "expected_currency_bytes": 'CURRENCY = "EUR"\n',
        "mechanism_acceptance": (
            []
            if scenario == "natural"
            else [
                "An initial write-capable coding child in isolation completes successfully before its continuation launches.",
                "subagent_resume receives that run_id and an explicit pickup follow-up task.",
                "The continuation retains the same worktree and prior history, with no duplicate first-phase application.",
                "The final continuation is explicitly applied; acceptance tests run against the parent workspace.",
            ]
        ),
        "interpretation": "Natural case imposes no child count. Follow-up mechanism is deliberately controlled. Compare correctness, retained dirty edit, verification evidence, elapsed time, usage/cache, and delegation trace separately. Failed attempts and host deadlines remain in the record.",
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


def check(destination: Path) -> int:
    manifest = json.loads((destination / "manifest.json").read_text())
    acceptance = destination / "acceptance.py"
    if hashlib.sha256(acceptance.read_bytes()).hexdigest() != manifest["acceptance_sha256"]:
        raise RuntimeError("Frozen acceptance file was modified")
    env = dict(os.environ)
    env["ORDERQUOTE_CASE_ROOT"] = manifest["workspace"]
    env["ORDERQUOTE_CASE_SCENARIO"] = manifest["scenario"]
    return subprocess.run([sys.executable, str(acceptance)], env=env, timeout=45).returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "check"])
    parser.add_argument("destination", type=Path)
    parser.add_argument("--scenario", choices=["natural", "followup"], default="natural")
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.destination.resolve(), args.scenario)
    else:
        raise SystemExit(check(args.destination.resolve()))
