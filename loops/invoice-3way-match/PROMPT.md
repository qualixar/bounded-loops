# Invoice 3-Way Match: fix invoice.json so it matches the PO and goods receipt

Goal: make `pytest -q` (run from `seed/`) pass by correcting `invoice.json`
against the ground-truth `purchase_order.json` and `goods_receipt.json`.

Steps each turn:
  1. Run: `pytest -q`
  2. If it fails, read the failure: it names the SKU and whether the
     mismatch is price (vs `purchase_order.json`) or quantity (vs
     `goods_receipt.json`).
  3. Edit `seed/invoice.json` so that line's `unit_price` matches the PO and
     `quantity` matches the goods receipt for that SKU.
  4. Run pytest again to confirm.

Done when: pytest reports 0 failures.
Then output: <promise>MATCHED</promise>

Do not edit `seed/test_three_way_match.py` — that is the gate, not the target.
Do not edit `seed/purchase_order.json` or `seed/goods_receipt.json` — they are
ground truth; conform the invoice to them, never the other way round.
Do not add new dependencies.
