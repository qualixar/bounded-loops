# seed/test_three_way_match.py — DO NOT EDIT (gate anchor)
# Python 3.11+
import json
from pathlib import Path

_DIR = Path(__file__).parent
PO_PATH = _DIR / "purchase_order.json"
GR_PATH = _DIR / "goods_receipt.json"
INVOICE_PATH = _DIR / "invoice.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _by_sku(lines: list[dict], qty_key: str) -> dict[str, dict]:
    return {line["sku"]: line for line in lines}


class TestThreeWayMatch:
    def test_every_invoice_line_matches_po_price(self):
        po = _by_sku(_load(PO_PATH)["lines"], "unit_price")
        invoice = _load(INVOICE_PATH)["lines"]
        for line in invoice:
            sku = line["sku"]
            assert sku in po, f"invoice line {sku!r} has no matching PO line"
            assert line["unit_price"] == po[sku]["unit_price"], (
                f"invoice line {sku!r} unit_price={line['unit_price']} "
                f"does not match PO unit_price={po[sku]['unit_price']}"
            )

    def test_every_invoice_line_matches_goods_receipt_quantity(self):
        gr = _by_sku(_load(GR_PATH)["lines"], "quantity_received")
        invoice = _load(INVOICE_PATH)["lines"]
        for line in invoice:
            sku = line["sku"]
            assert sku in gr, f"invoice line {sku!r} has no matching goods receipt line"
            assert line["quantity"] == gr[sku]["quantity_received"], (
                f"invoice line {sku!r} quantity={line['quantity']} does not match "
                f"goods receipt quantity_received={gr[sku]['quantity_received']}"
            )
