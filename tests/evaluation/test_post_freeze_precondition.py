"""The precondition that makes a content-removal operator's DESTROYING claim certain.

Derived from adjudicating E7's first run: 21 apparent false accepts, of which a spec review found
7 mislabelled. The 7 were two failures of one assumption, and this pins the rule that replaced it.

Every case below is a real loop from the catalogue and the verdict is the one recorded in
`E7-ADJUDICATION.md`, so a change to the heuristic that reopens a mislabelling fails here rather
than moving a published rate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from bounded_loops.evaluation.operators.post_freeze import is_content_quantified

_CATALOG = Path(__file__).resolve().parents[2] / "loops"

#: loop -> may a content-removal operator claim "destroying" for this requirement?
#:
#: True  = the requirement is universally quantified over content the operator removes, so
#:         emptying it leaves the claim vacuously satisfied and the artifact useless.
#: False = the claim is NOT certain, so no mutant may be emitted. Two shapes:
#:         negative requirements, where removing content SATISFIES them; and requirements met by
#:         structure the operator preserves (a name, a signature).
_ADJUDICATED: dict[str, bool] = {
    # quantified over removed content -> destroying is certain
    "dataset-license-allowed": True,        # every dataset's license is in the allowlist
    "gtin-checkdigit": True,                # every product's GTIN-13 check digit is valid
    "inventory-nonnegative": True,          # every SKU's running balance stays non-negative
    "price-margin-floor": True,             # every SKU's price is at or above its floor
    "transport-request-manifest": True,     # every dependency is covered in the manifest
    "gdpr-dpa-terms": True,                 # every mandatory Art.28(3) term is present
    "nda-required-clauses": True,           # every required clause is present
    "privacy-policy-completeness": True,    # every required section is present
    # negative requirement -> removing content satisfies it, so the gate is right to pass
    "cors-not-wildcard": False,             # credentials NEVER pair with '*'
    "no-hardcoded-sleep": False,            # NO test contains a hardcoded time.sleep
    # satisfied by structure the operator preserves
    "test-naming-contract": False,          # every test-like function is NAMED test_*
    "type-annotations-present": False,      # every public function is ANNOTATED
    # the requirement IS the schema, and an emptied document still validates against it
    "bapi-payload-contract": False,
    "catalog-required-fields": False,
}


def _stated_purpose(loop: str) -> str:
    prompt = _CATALOG / loop / "PROMPT.md"
    if not prompt.exists():
        pytest.skip(f"{loop} has no PROMPT.md")
    text = prompt.read_text(encoding="utf-8")
    match = re.search(r"Goal:(.*?)(?:\n\n|\nSteps)", text, re.S)
    return " ".join((match.group(1) if match else text).split())


@pytest.mark.parametrize("loop,may_destroy", sorted(_ADJUDICATED.items()))
def test_the_precondition_matches_the_spec_review(loop: str, may_destroy: bool) -> None:
    assert is_content_quantified(_stated_purpose(loop)) is may_destroy, (
        f"{loop}: the precondition disagrees with the recorded adjudication. If the requirement "
        "text changed, re-adjudicate and update E7-ADJUDICATION.md; do not loosen the heuristic "
        "to fit, because every loosening admits a mutant nobody can label into a denominator."
    )


def test_the_precondition_fails_closed_on_an_unrecognised_requirement() -> None:
    """An unparseable or silent requirement yields no mutant, never a guessed one.

    This is the same discipline the product applies to gates: the honest output of an unmeasurable
    question is a refusal. It costs real coverage -- `slo-error-budget` states its goal as "make
    the test pass", names no quantified obligation, and is withheld even though the spec review
    judged its mutant genuinely defective. One true positive lost is the correct price for zero
    mislabelled trials admitted.
    """
    assert is_content_quantified("") is False
    assert is_content_quantified("make the test in seed/test_error_budget.py pass.") is False
    assert is_content_quantified("do the needful") is False


def test_a_quantified_requirement_is_recognised_without_a_loop_on_disk() -> None:
    assert is_content_quantified("report that every record carries a category") is True
    assert is_content_quantified("report that each entry resolves") is True


def test_a_negative_requirement_is_refused_even_when_it_also_quantifies() -> None:
    """`no test contains ...` quantifies AND is negative. Negative must win, or the operator
    emits exactly the mutant the E7 review found mislabelled."""
    assert is_content_quantified("report that no test contains a hardcoded sleep in every file") is False
