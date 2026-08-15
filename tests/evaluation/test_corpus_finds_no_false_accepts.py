"""The corpus result, pinned. Without this it is a number I once observed.

Every defect closed this cycle had the same shape: something real that nothing asserted. The
anytime-valid confidence sequence was implemented, tested, and called by nothing. The provider
evidence lived in comments saying "verified live" that no reader could re-check. The approval
attribution shipped with no test naming `decided_by`. Twelve gates accepted emptied artifacts while
the suite stayed green.

Leaving "0 false accepts" as something measured by hand would repeat exactly that, on the result
the paper's central claim rests on.

**What is asserted:** every mutant the corpus considers judgeable gets the verdict its construction
label demands. A destroyed artifact is rejected; a semantics-preserving edit is accepted. A single
false accept fails this test and names the loop and operator.

**Cost.** This converges each loop and runs a gate per mutant, so it is minutes, not seconds, and
carries `external_tool` — it shells out to the loop runner. Run it before a release and when a gate
or an operator changes:

    pytest tests/evaluation/test_corpus_finds_no_false_accepts.py -m external_tool

Deselected by default for runtime alone. That is a real tension: this file argues that unpinned
results rot, and a deselected test is one marker away from unpinned. The mitigation is that the
release gate runs it explicitly, and `test_the_corpus_is_not_silently_empty` below runs by default
and fails if the corpus ever collapses to nothing.
"""

from __future__ import annotations

from pathlib import Path
import tempfile

import pytest

from bounded_loops.evaluation import corpus, harness

_CATALOG = Path(__file__).resolve().parents[2] / "loops"


def _eligible_loops() -> list[Path]:
    """Loops where the Tier-1 destroying claim is certain. Every exclusion is a stated reason."""
    return [
        loop_dir for loop_dir in corpus.iter_loop_dirs(_CATALOG)
        if not harness.states_a_negative_requirement(loop_dir.name)
        and not harness.uses_an_external_tool(loop_dir)
    ]


@pytest.mark.external_tool
def test_no_gate_accepts_a_destroyed_artifact() -> None:
    """α = 0 over the Tier-1 corpus, or the failure names exactly which gate broke."""
    false_accepts: list[str] = []
    false_rejects: list[str] = []
    judged = 0
    loops_covered = 0

    with tempfile.TemporaryDirectory(prefix="bl-corpus-test-") as scratch:
        for loop_dir in _eligible_loops():
            baseline = harness.establish_baseline(loop_dir, into=Path(scratch) / loop_dir.name)
            if baseline is None:
                continue

            mutants = [
                mutant
                for mutant in corpus.generate_for_loop(loop_dir, content_root=baseline)
                if harness.judges_artifact(loop_dir, mutant.mutation.path)
            ]
            if not harness.tier1_claim_holds(len({m.mutation.path for m in mutants})):
                continue
            if mutants:
                loops_covered += 1

            for mutant in mutants:
                outcome = harness.run_mutant(mutant, catalog_root=_CATALOG, baseline=baseline)
                if not outcome.counts_toward_a_rate:
                    continue
                judged += 1
                if outcome.is_false_accept:
                    false_accepts.append(f"{outcome.loop}: {outcome.operator}")
                if outcome.is_false_reject:
                    false_rejects.append(f"{outcome.loop}: {outcome.operator}")

    assert judged > 0, "the corpus judged nothing; it is measuring no gates at all"
    assert loops_covered >= 20, (
        f"only {loops_covered} loops carried mutants — coverage collapsed, so a zero here would "
        "mean 'nothing was checked' rather than 'nothing was wrong'"
    )
    assert not false_accepts, (
        f"{len(false_accepts)} gate(s) accepted an artifact that was destroyed by construction:\n  "
        + "\n  ".join(sorted(false_accepts))
    )
    assert not false_rejects, (
        f"{len(false_rejects)} gate(s) rejected a semantics-preserving edit:\n  "
        + "\n  ".join(sorted(false_rejects))
    )


def test_the_corpus_is_not_silently_empty() -> None:
    """Runs by DEFAULT, and is the guard against the guard above being deselected into nothing.

    Generation needs no gate and no loop run, so it is fast. If an operator change ever made the
    corpus produce nothing, the expensive test would pass vacuously the next time anyone ran it —
    the same vacuous-pass defect this whole corpus exists to hunt.
    """
    mutants = corpus.generate(_CATALOG)

    assert len(mutants) >= 100, f"corpus collapsed to {len(mutants)} mutants"
    assert len({m.loop for m in mutants}) >= 40, "corpus no longer spans the catalog"

    labels = {m.mutation.label for m in mutants}
    assert labels == {"correct", "incorrect"}, (
        f"a whole label family disappeared: {labels}. One-sided evidence cannot measure both a "
        "false-accept and a false-reject rate."
    )


def test_every_excluded_loop_has_a_stated_reason() -> None:
    """Exclusions must be principled and enumerable, never 'whatever did not work'.

    A corpus that silently drops the loops it finds awkward reports a rate for a population it
    chose after seeing the results.
    """
    all_loops = {loop_dir.name for loop_dir in corpus.iter_loop_dirs(_CATALOG)}
    eligible = {loop_dir.name for loop_dir in _eligible_loops()}
    excluded = all_loops - eligible

    for name in excluded:
        loop_dir = _CATALOG / name
        assert (
            harness.states_a_negative_requirement(name)
            or harness.uses_an_external_tool(loop_dir)
        ), f"{name} is excluded for no stated reason"
