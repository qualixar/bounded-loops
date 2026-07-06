# seed/test_ledger.py — DO NOT EDIT (gate anchor)
# Python 3.11+
import csv
from pathlib import Path

LEDGER_PATH = Path(__file__).parent / "ledger.csv"
ALLOWED_CATEGORIES = {"equity", "expense", "revenue", "asset", "liability"}


def _read_rows() -> list[dict[str, str]]:
    with LEDGER_PATH.open(newline="") as f:
        return list(csv.DictReader(f))


class TestLedgerReconciliation:
    def test_debits_equal_credits(self):
        rows = _read_rows()
        total_debit = sum(float(r["debit"]) for r in rows if r["debit"].strip())
        total_credit = sum(float(r["credit"]) for r in rows if r["credit"].strip())
        assert total_debit == total_credit, (
            f"ledger does not balance: total_debit={total_debit} "
            f"total_credit={total_credit}"
        )

    def test_every_transaction_categorized(self):
        rows = _read_rows()
        for r in rows:
            category = r["category"].strip()
            assert category, f"row id={r['id']} has an empty category"
            assert category in ALLOWED_CATEGORIES, (
                f"row id={r['id']} has invalid category {category!r}; "
                f"allowed={sorted(ALLOWED_CATEGORIES)}"
            )
