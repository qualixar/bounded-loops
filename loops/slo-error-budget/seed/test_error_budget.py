# seed/test_error_budget.py — DO NOT EDIT (gate anchor)
# Python 3.11+
import json
import math
from pathlib import Path

SLO_PATH = Path(__file__).parent / "slo.json"


class TestErrorBudget:
    def test_budget_remaining_matches_allowed_minus_downtime(self):
        """
        The error budget for an SLO window is:
            allowed = window_minutes * (1 - slo_target_pct / 100)
            budget_remaining_minutes = allowed - downtime_minutes

        A wrong budget_remaining_minutes understates or overstates how much
        downtime is left before the SLO is breached — either false comfort
        or an unnecessary page. This test is the ground truth: it recomputes
        the correct value from the other three fields and asserts slo.json
        reports exactly that.
        """
        data = json.loads(SLO_PATH.read_text(encoding="utf-8"))

        window_minutes = data["window_minutes"]
        slo_target_pct = data["slo_target_pct"]
        downtime_minutes = data["downtime_minutes"]
        budget_remaining_minutes = data["budget_remaining_minutes"]

        allowed = window_minutes * (1 - slo_target_pct / 100)
        expected_remaining = allowed - downtime_minutes

        assert math.isclose(
            budget_remaining_minutes, expected_remaining, rel_tol=1e-9, abs_tol=1e-9
        ), (
            f"budget_remaining_minutes={budget_remaining_minutes} does not match "
            f"allowed({allowed}) - downtime_minutes({downtime_minutes}) "
            f"= {expected_remaining}"
        )
