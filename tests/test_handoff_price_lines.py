import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app" / "handoff_price_lines.js"


def _run_collector(rows: list[dict[str, str]]) -> dict[str, object]:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required to execute the browser price-line helper")
    script = r"""
const collector = require(process.argv[1]);
const rows = JSON.parse(process.argv[2]);
function fakeInput(value) { return {value}; }
function fakeRow(values) {
  return {
    querySelector(selector) {
      if (selector === ".manual-price-product") {
        return {
          value: values.product_id,
          selectedOptions: values.product_id
            ? [{textContent: values.label || values.product_id}]
            : [],
        };
      }
      if (selector === ".manual-price-value") return fakeInput(values.price);
      if (selector === ".manual-price-quantity") return fakeInput(values.quantity);
      throw new Error(`unexpected selector: ${selector}`);
    },
  };
}
const container = {
  querySelectorAll(selector) {
    if (selector !== "[data-price-line]") throw new Error(`unstable selector: ${selector}`);
    return rows.map(fakeRow);
  },
};
try {
  console.log(JSON.stringify({ok: true, result: collector.collectPriceLines(container)}));
} catch (error) {
  console.log(JSON.stringify({ok: false, name: error.name, message: error.message}));
}
"""
    completed = subprocess.run(
        ["node", "-e", script, str(MODULE_PATH), json.dumps(rows)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_price_line_collector_uses_stable_rows_and_ignores_default_empty_line() -> None:
    result = _run_collector(
        [
            {"product_id": "", "price": "", "quantity": ""},
            {
                "product_id": "12",
                "label": "WIDGET-12",
                "price": "45.25",
                "quantity": "7",
            },
        ]
    )

    assert result == {
        "ok": True,
        "result": {
            "lines": [
                {
                    "product_id": 12,
                    "standard_price": 45.25,
                    "quantity": 7,
                }
            ],
            "labels": ["WIDGET-12: INR 45.25 x 7"],
        },
    }


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (
            [
                {"product_id": "12", "price": "1", "quantity": "1"},
                {"product_id": "12", "price": "2", "quantity": "2"},
            ],
            "Duplicate products are not allowed",
        ),
        (
            [{"product_id": "12", "price": "1", "quantity": "1.5"}],
            "Quantity must be a positive integer",
        ),
        (
            [{"product_id": "12", "price": "0", "quantity": "1"}],
            "Price must be greater than zero",
        ),
        (
            [{"product_id": "", "price": "1", "quantity": "1"}],
            "Select a product",
        ),
    ],
)
def test_price_line_collector_rejects_invalid_or_duplicate_rows(
    rows: list[dict[str, str]],
    message: str,
) -> None:
    result = _run_collector(rows)

    assert result["ok"] is False
    assert result["name"] == "PriceLineValidationError"
    assert result["message"] == message
