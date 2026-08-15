""""The checker was withheld" is arithmetic here, not an assurance.

Tier 1's blindness is enforced against source: the generator provably cannot reach a gate, because
an AST walk says so. Tier 2 cannot work that way — a human or a model did the authoring, and no
test can inspect what they read.

So the claim is made checkable instead of provable. Each mutant records the digest of exactly what
its author was shown, and that digest is recomputable from this repository by anyone. If someone
widened the inputs to include `seed/check_*.py`, the recorded digest stops matching what
`authoring_prompt_digest` produces, and these tests fail.

That is a weaker guarantee than Tier 1's and it is stated as weaker. It cannot prove nobody peeked.
It can prove that what they say they saw is what the repository still contains — and it makes any
later widening of the inputs a visible, failing event rather than a silent one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bounded_loops.evaluation import tier2

_CATALOG = Path(__file__).resolve().parents[2] / "loops"
_CORPUS = _CATALOG.parent / "bounded_loops" / "evaluation" / tier2.CORPUS_FILENAME


def _corpus() -> list[tier2.Tier2Mutant]:
    if not _CORPUS.is_file():
        pytest.skip(f"no Tier-2 corpus at {_CORPUS.name} yet")
    return tier2.load(_CORPUS)


# ── what an author is shown ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "loop_dir",
    sorted(p.parent for p in _CATALOG.glob("*/loop.yaml")),
    ids=lambda p: p.name,
)
def test_the_authoring_prompt_never_contains_a_checker(loop_dir: Path) -> None:
    """The prompt is built from `PROMPT.md` and `README.md`. Neither may leak the implementation.

    Checked against the real catalog rather than the builder's source, because the leak that
    matters is a checker QUOTED inside a README — which no amount of care in `authoring_prompt`
    would prevent, and which would hand the author exactly what is meant to be withheld.
    """
    prompt = tier2.authoring_prompt(loop_dir)

    for checker in sorted(loop_dir.glob("seed/check_*.py")):
        body = checker.read_text(encoding="utf-8")
        # Any substantial verbatim run from the checker appearing in the prompt is a leak. Compared
        # by stripped code lines so that a shared sentence of prose does not read as one.
        code_lines = [
            line.strip() for line in body.splitlines()
            if line.strip() and not line.strip().startswith(("#", '"""', "'''"))
        ]
        leaked = [line for line in code_lines if len(line) > 30 and line in prompt]
        assert not leaked, (
            f"{loop_dir.name}: {checker.name} appears inside the authoring prompt:\n  "
            + "\n  ".join(leaked[:3])
        )


def test_the_prompt_digest_changes_when_the_inputs_change(tmp_path: Path) -> None:
    """The digest must actually be sensitive to what it summarises.

    Without this, every mutant could carry a constant and the provenance check below would pass
    while proving nothing — a vacuous guard, which is the defect this whole corpus hunts.
    """
    loop = tmp_path / "a-loop"
    loop.mkdir()
    (loop / "PROMPT.md").write_text("do the thing", encoding="utf-8")
    (loop / "README.md").write_text("about the thing", encoding="utf-8")

    before = tier2.authoring_prompt_digest(loop)
    (loop / "PROMPT.md").write_text("do the thing, differently", encoding="utf-8")
    after = tier2.authoring_prompt_digest(loop)

    assert before != after, "the digest ignores its own inputs"


def test_a_widened_prompt_is_detectable(tmp_path: Path) -> None:
    """Adding the checker to a loop's visible files must move the digest.

    This is the exact evasion the provenance record exists to catch: an author who quietly reads
    the implementation and reports the old digest.
    """
    loop = tmp_path / "a-loop"
    (loop / "seed").mkdir(parents=True)
    (loop / "PROMPT.md").write_text("do the thing", encoding="utf-8")
    (loop / "README.md").write_text("about the thing", encoding="utf-8")
    honest = tier2.authoring_prompt_digest(loop)

    # Simulate a widened input set by appending the checker into a visible file.
    (loop / "README.md").write_text(
        "about the thing\n\n```\nif not violations: return 0\n```", encoding="utf-8"
    )

    assert tier2.authoring_prompt_digest(loop) != honest


# ── the committed corpus, once it exists ─────────────────────────────────────


def test_every_mutant_records_a_prompt_digest_that_still_matches() -> None:
    """The provenance check. A mutant whose recorded digest no longer matches the repository was
    authored from inputs that have since changed — or from inputs that were never those files."""
    stale: list[str] = []
    for mutant in _corpus():
        expected = tier2.authoring_prompt_digest(_CATALOG / mutant.loop)
        if mutant.prompt_digest != expected:
            stale.append(f"{mutant.mutant_id}: recorded {mutant.prompt_digest[:19]}, repo has {expected[:19]}")

    assert not stale, (
        "mutants whose authoring provenance no longer matches this repository:\n  "
        + "\n  ".join(stale)
    )


def test_every_mutant_names_the_requirement_it_violates() -> None:
    """An unexplained label cannot be reviewed, and Tier 1 proved unreviewed labels go wrong."""
    for mutant in _corpus():
        assert len(mutant.requirement.strip()) > 15, (
            f"{mutant.mutant_id}: requirement {mutant.requirement!r} is too thin for a reviewer "
            "to check against PROMPT.md"
        )


def test_the_corpus_is_not_dominated_by_one_author() -> None:
    """Model diversity is the point of authoring across several CLIs.

    One author's blind spots become the corpus's blind spots, and a corpus that cannot see a class
    of defect reports a lower false-accept rate for exactly the wrong reason.
    """
    mutants = _corpus()
    if len(mutants) < 10:
        pytest.skip("corpus too small for a diversity claim")

    counts: dict[str, int] = {}
    for mutant in mutants:
        counts[mutant.authored_by] = counts.get(mutant.authored_by, 0) + 1

    largest = max(counts.values())
    assert largest / len(mutants) <= 0.75, (
        f"one author wrote {largest}/{len(mutants)} of the corpus: {counts}. Its blind spots are "
        "now the corpus's blind spots."
    )
