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
        if harness.excluded_reason(loop_dir) is None
    ]


@pytest.mark.external_tool
def test_no_gate_accepts_a_destroyed_artifact() -> None:
    """α = 0 over the Tier-1 corpus, or the failure names exactly which gate broke.

    **An eligible loop that produces nothing fails this test.** It used to `continue`, which is how
    twenty-four loops — every `jsonschema`, `pytest` and `composite` gate in the catalog — left the
    measurement without appearing in any count. "No mutants" and "no false accepts" are
    indistinguishable in a total, so silence there reads as success.
    """
    false_accepts: list[str] = []
    false_rejects: list[str] = []
    produced_nothing: list[str] = []
    errored: list[str] = []
    judged = 0
    loops_covered = 0

    with tempfile.TemporaryDirectory(prefix="bl-corpus-test-") as scratch:
        for loop_dir in _eligible_loops():
            baseline = harness.establish_baseline(loop_dir, into=Path(scratch) / loop_dir.name)
            if baseline is None:
                produced_nothing.append(f"{loop_dir.name}: no baseline (did not converge, or its "
                                        "gate rejects its own converged artifact)")
                continue

            mutants = [
                mutant
                for mutant in corpus.generate_for_loop(loop_dir, content_root=baseline)
                if harness.judges_artifact(loop_dir, mutant.mutation.path)
            ]
            judged_paths = len({m.mutation.path for m in mutants})
            if not harness.tier1_claim_holds(judged_paths):
                # A stated methodological limit, not a silent drop: with several judged artifacts
                # the requirement is about their relation, so emptying one can satisfy it vacuously.
                assert judged_paths != 1, "unreachable: tier1_claim_holds(1) is True"
                continue
            if not mutants:
                produced_nothing.append(f"{loop_dir.name}: gate judges no mutable artifact")
                continue
            loops_covered += 1

            for mutant in mutants:
                outcome = harness.run_mutant(mutant, catalog_root=_CATALOG, baseline=baseline)
                if not outcome.counts_toward_a_rate:
                    errored.append(f"{outcome.loop}: {outcome.operator} — {outcome.detail[:90]}")
                    continue
                judged += 1
                if outcome.is_false_accept:
                    false_accepts.append(f"{outcome.loop}: {outcome.operator}")
                if outcome.is_false_reject:
                    false_rejects.append(f"{outcome.loop}: {outcome.operator}")

    assert not produced_nothing, (
        f"{len(produced_nothing)} loop(s) are eligible but contributed no mutants. Each is either "
        "a defect to fix or an exclusion to STATE in harness.excluded_reason — never a quiet "
        "`continue`:\n  " + "\n  ".join(sorted(produced_nothing))
    )
    # Errors are excluded from every rate, which makes them the place evidence hides. 84 of 233
    # mutants were once errors — every one a gate that had DETECTED a destroyed artifact and filed
    # the answer under "could not run", so 36% of the corpus left α while the remaining rate looked
    # perfect. Asserted at zero rather than "few", because there is no longer any reason for one:
    # every artifact in this corpus is worker-owned, and a gate that cannot judge worker output has
    # a defect. See docs/gate-verdict-contract.md.
    assert not errored, (
        f"{len(errored)} mutant(s) produced a gate ERROR rather than a verdict. An error is "
        "excluded from α, so this silently shrinks the denominator. Each is either a gate that "
        "should reject the artifact or an exclusion to state:\n  " + "\n  ".join(sorted(errored))
    )
    assert judged > 0, "the corpus judged nothing; it is measuring no gates at all"
    assert loops_covered >= 55, (
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

    **This test used to pass vacuously over the loops that mattered most.** It only inspected loops
    already outside `_eligible_loops()`, and the two predicates defining that set both read a
    `command` gate's argv. A `jsonschema` or `pytest` loop has no command, so `uses_an_external_tool`
    said False, the loop was ELIGIBLE, and this test never looked at it — while the run itself
    dropped all twenty-four of them for want of a gate it could build.

    The guard against unprincipled exclusion was itself vacuous, on exactly the class of defect the
    corpus exists to hunt. So the assertion now runs over the WHOLE catalog and demands that every
    loop is either measurable or carries a reason, rather than trusting a set that was built from
    the same predicates it is checking.
    """
    thin = [
        f"{loop_dir.name}: {reason!r}"
        for loop_dir in corpus.iter_loop_dirs(_CATALOG)
        if (reason := harness.excluded_reason(loop_dir)) is not None
        and len(reason.strip()) <= 20
    ]
    assert not thin, (
        "exclusion reasons too thin for a reviewer to argue with:\n  " + "\n  ".join(thin)
    )

    # The positive half. Without it this passes when excluded_reason returns None for everything —
    # which is the same vacuity, one level up: a guard against unstated exclusions that is
    # satisfied by there being no exclusions to state, including the ones actually happening.
    stated = {
        loop_dir.name
        for loop_dir in corpus.iter_loop_dirs(_CATALOG)
        if harness.excluded_reason(loop_dir) is not None
    }
    assert stated, (
        "no loop is excluded for any reason. Three scanners, one negative-requirement loop and "
        "three unshipped-SDK runners are known to be unmeasurable — if none are reported, this "
        "guard has stopped reading the catalog rather than the catalog having become perfect."
    )


def test_the_unshipped_package_exclusions_match_reality() -> None:
    """The excluded-for-a-missing-SDK set must describe THIS environment, in both directions.

    Whether a loop converges depends on what is installed, so an unverified exclusion list makes
    α's denominator a property of whoever ran the corpus. `autogen-example` proved it concretely:
    it converged under a system interpreter carrying `agent-framework` and failed under the project
    venv without it, and the run reported a different population each time while saying nothing.

    A reviewer recomputing our numbers on a clean checkout must get our denominator or a failure —
    never a quietly different one.

    Both halves matter. If a named package IS installed, that loop is now measurable and belongs in
    α, so leaving it excluded understates coverage. If some OTHER loop needs one, the list is
    incomplete and the drop is silent again.
    """
    from importlib.metadata import PackageNotFoundError, version

    installed_but_excluded: list[str] = []
    for loop_name, package in harness._RUNNER_NEEDS_UNSHIPPED_PACKAGE.items():
        try:
            found = version(package)
        except PackageNotFoundError:
            continue
        installed_but_excluded.append(f"{loop_name}: {package} {found} IS installed")

    assert not installed_but_excluded, (
        "these loops are excluded for a package that is present, so they can now be measured and "
        "the corpus is understating its coverage. Remove them from "
        "_RUNNER_NEEDS_UNSHIPPED_PACKAGE:\n  " + "\n  ".join(sorted(installed_but_excluded))
    )


def test_the_stated_exclusions_are_a_small_named_set() -> None:
    """Exclusions must stay rare and enumerable, or the reported α describes a rump of the catalog.

    Pinned by COUNT as well as by reason, because "every exclusion has a reason" is satisfiable by
    excluding everything with reasons. The population the rate covers is part of the claim.
    """
    excluded = {
        loop_dir.name: harness.excluded_reason(loop_dir)
        for loop_dir in corpus.iter_loop_dirs(_CATALOG)
        if harness.excluded_reason(loop_dir) is not None
    }
    total = sum(1 for _ in corpus.iter_loop_dirs(_CATALOG))

    assert len(excluded) <= 10, (
        f"{len(excluded)} of {total} loops are excluded from α: {sorted(excluded)}. Any α computed "
        "over what remains describes a population chosen by this list."
    )
    assert total - len(excluded) >= 58, (
        f"only {total - len(excluded)} loops remain eligible out of {total}"
    )
