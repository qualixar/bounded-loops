"""The Tier-2 result, pinned — semantic mutants authored from the stated purpose, run past real gates.

Tier 1 asks one mechanical question of every gate: does it notice when the artifact is destroyed?
That claim is certain without reading anything, which is what makes it blind and what makes it
shallow. It cannot say "this clause now says the opposite", "this test imports a different module",
or "this dependency was deleted rather than pinned".

Tier 2 can, and the first run found **21 acceptances across 10 loops** that Tier 1 had reported
clean — including four defects in `test-presence-per-module`, a loop Tier 1 excludes entirely for
having several judged artifacts.

**What is asserted:** every mutant is rejected, except those on two explicitly reviewed lists in
`tier2.py`. Both lists are keyed by content digest, so a mutant cannot drift into an exemption by
being edited, and both carry a written reason a reader can check against the mutated text.

* `MUTANT_IS_MISLABELLED` — the author was wrong and the gate was right.
* `NOT_MECHANICALLY_CHECKABLE` — the requirement is real and no keyless gate can verify it.

Keeping those separate is the point. Collapsing them into one "known failures" list would hide the
difference between a corpus error and a boundary of the method, and the second is the finding.

**Cost.** Converges each loop and runs a gate per mutant, so it carries `external_tool` alongside
the Tier-1 corpus test and runs at the release gate:

    pytest tests/evaluation/ -m external_tool
"""

from __future__ import annotations

from pathlib import Path
import tempfile

import pytest

from bounded_loops.evaluation import corpus, harness, tier2

_CATALOG = Path(__file__).resolve().parents[2] / "loops"
_CORPUS = _CATALOG.parent / "bounded_loops" / "evaluation" / tier2.CORPUS_FILENAME


def _corpus() -> list[tier2.Tier2Mutant]:
    if not _CORPUS.is_file():
        pytest.skip(f"no Tier-2 corpus at {_CORPUS.name} yet")
    return tier2.load(_CORPUS)


@pytest.mark.external_tool
def test_every_semantic_mutant_is_rejected_or_explicitly_excused() -> None:
    """The Tier-2 α, or the failure names exactly which gate accepted what."""
    mutants = _corpus()
    excused = {**tier2.MUTANT_IS_MISLABELLED, **tier2.NOT_MECHANICALLY_CHECKABLE}

    by_loop: dict[str, list[tier2.Tier2Mutant]] = {}
    for mutant in mutants:
        by_loop.setdefault(mutant.loop, []).append(mutant)

    accepted: list[str] = []
    unreachable: list[str] = []
    excused_but_rejected: list[str] = []
    judged = 0

    with tempfile.TemporaryDirectory(prefix="bl-tier2-") as scratch:
        for loop, group in sorted(by_loop.items()):
            baseline = harness.establish_baseline(_CATALOG / loop, into=Path(scratch) / loop)
            if baseline is None:
                unreachable.append(f"{loop}: no baseline, so its mutants measured nothing")
                continue
            for mutant in group:
                outcome = harness.run_mutant(
                    corpus.Mutant(loop=loop, mutation=mutant.as_mutation()),
                    catalog_root=_CATALOG, baseline=baseline,
                )
                if not outcome.counts_toward_a_rate:
                    unreachable.append(f"{loop}: {mutant.path} errored — {outcome.detail[:80]}")
                    continue
                judged += 1
                if outcome.is_false_accept and mutant.digest not in excused:
                    accepted.append(
                        f"{loop} [{mutant.authored_by}] {mutant.path}: {mutant.requirement[:110]}"
                    )
                if not outcome.is_false_accept and mutant.digest in excused:
                    excused_but_rejected.append(f"{loop} [{mutant.authored_by}] {mutant.path}")

    assert not unreachable, (
        "Tier-2 mutants that produced no verdict — each measured nothing:\n  "
        + "\n  ".join(sorted(unreachable))
    )
    assert judged == len(mutants), f"only {judged} of {len(mutants)} mutants were judged"
    assert not accepted, (
        f"{len(accepted)} gate(s) accepted work that violates their loop's STATED PURPOSE. Each is "
        "either a defect to fix or a reviewed entry to add to tier2.MUTANT_IS_MISLABELLED / "
        "NOT_MECHANICALLY_CHECKABLE with a reason:\n  " + "\n  ".join(sorted(accepted))
    )
    assert not excused_but_rejected, (
        "these mutants are on an exemption list but are now REJECTED, so the exemption is stale "
        "and is suppressing a gate that works. Remove them from tier2.py:\n  "
        + "\n  ".join(sorted(excused_but_rejected))
    )


def test_the_exemption_lists_stay_small_and_reasoned() -> None:
    """Runs by DEFAULT. An exemption list is how a corpus quietly stops measuring.

    Nothing stops "excuse it" from becoming the response to every finding, and the expensive test
    above would keep passing the whole way down. So the lists are bounded here, by default, where
    anyone adding to them sees the ceiling.
    """
    mutants = _corpus()
    excused = {**tier2.MUTANT_IS_MISLABELLED, **tier2.NOT_MECHANICALLY_CHECKABLE}

    assert len(excused) <= len(mutants) // 4, (
        f"{len(excused)} of {len(mutants)} mutants are excused. Past a quarter, the corpus is "
        "reporting a rate for whatever was left after the awkward cases were annotated away."
    )
    for digest, reason in excused.items():
        assert digest.startswith("sha256:"), f"{digest} is not a content digest"
        assert len(reason) > 80, (
            f"{digest[:19]}: reason is too thin to review — it must say what the mutant does and "
            "why the gate is right, checkable against the mutated text without running anything"
        )

    known = {mutant.digest for mutant in mutants}
    orphans = sorted(digest for digest in excused if digest not in known)
    assert not orphans, (
        "exemptions naming mutants that are not in the corpus — a stale list is an exemption "
        f"nobody can check:\n  {orphans}"
    )


def test_the_two_exemption_lists_do_not_overlap() -> None:
    """A mutant is either mislabelled or unfixable, never filed as both.

    The distinction carries the finding: one says the corpus was wrong, the other says mechanical
    gating has a boundary. A digest in both lists means nobody decided which.
    """
    both = set(tier2.MUTANT_IS_MISLABELLED) & set(tier2.NOT_MECHANICALLY_CHECKABLE)
    assert not both, f"filed as both mislabelled and unfixable: {sorted(both)}"
