"""``bl graph plan`` must report the pre-run work bound, for every graph this repo ships.

Two defects motivated this file and each test here fails against the code as it stood.

1. ``cmd_graph_plan`` printed ``plan_id``, digests, waves and per-node effects, and NOT the bound
   — while the paper whose front door this command is has "Pre-Run Spend Bounds" in its title.
   ``total_execution_bound`` had zero non-test callers in the whole package.

2. ``cmd_graph_plan`` passed ``package_digests=frozenset()``, so ``_validate_packages`` refused
   every ``kind: loop`` node and therefore all six shipped reference graphs, while ``bl graph run``
   admitted the local catalogue. The pre-run command could not inspect the graphs the runner
   accepts. Eleven other call sites already used ``admitted_loop_package_digests``; this one did
   not, which is why no unit test caught it — each of those eleven proves only itself.

The parity test is the load-bearing one: a plan that admits a different package set than the run
is a plan that lies about the run it describes.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pytest

from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.repair_rounds import total_execution_bound
from bounded_loops.graph.application.validate_graph import parse_authoring_graph_yaml
from bounded_loops.graph.cli_graph import cmd_graph_plan, cmd_graph_run, register
from bounded_loops.graph.domain.authoring import _NULL_POLICY_DIGEST
from bounded_loops.graph.loop_node_wiring import admitted_loop_package_digests, parse_loop_roots

REPO_ROOT = Path(__file__).resolve().parents[2]
SHIPPED_GRAPHS = sorted((REPO_ROOT / "graphs").glob("*/graph.yaml"))


def _ns(manifest: Path, **kw: object) -> argparse.Namespace:
    kw.setdefault("json", False)
    kw.setdefault("connections", None)
    kw.setdefault("loop_roots", None)
    return argparse.Namespace(manifest=str(manifest), **kw)


def test_the_repo_actually_ships_reference_graphs() -> None:
    """Guard the parametrisation itself: a bad glob would silently make the suite below vacuous."""
    assert len(SHIPPED_GRAPHS) >= 6, f"expected the six reference graphs, found {SHIPPED_GRAPHS}"


@pytest.mark.parametrize("manifest", SHIPPED_GRAPHS, ids=lambda p: p.parent.name)
def test_plan_admits_every_shipped_reference_graph(
    manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first command a reader arriving from the paper runs. It must not refuse our own graphs."""
    rc = cmd_graph_plan(_ns(manifest))
    captured = capsys.readouterr()
    assert rc == 0, f"{manifest.parent.name} was refused: {captured.err.strip()}"

    # Assert the NUMBER, not the label. Checking only for the substring "bound   :" passed even
    # against a hardcoded line of zeros, which the Wave-1 audit demonstrated.
    graph = parse_authoring_graph_yaml(manifest.read_text(encoding="utf-8"))
    plan = compile_graph(
        graph,
        CompileSnapshot(
            policy_digest=_NULL_POLICY_DIGEST,
            package_digests=admitted_loop_package_digests(parse_loop_roots(None)),
            connections=(),
        ),
    )
    assert f"bound   : {total_execution_bound(plan)} attempt slots max" in captured.out, (
        f"{manifest.parent.name} printed a bound that is not the authority's: {captured.out}"
    )


@pytest.mark.parametrize("manifest", SHIPPED_GRAPHS, ids=lambda p: p.parent.name)
def test_printed_bound_is_the_authority_not_a_recomputation(
    manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The number on screen must be ``total_execution_bound``'s, or display and bound can drift."""
    namespace = _ns(manifest, json=True)
    assert cmd_graph_plan(namespace) == 0
    payload = json.loads(capsys.readouterr().out)

    # Recompile the same way the command does, then compare against the authority directly.
    graph = parse_authoring_graph_yaml(manifest.read_text(encoding="utf-8"))
    # Resolve roots from the SAME namespace the command received. Hardcoding `parse_loop_roots(None)`
    # here — as this test did until the Wave-1 audit — meant the assertion held even if the command
    # ignored `args.loop_roots` entirely, so it could not detect broken flag wiring.
    plan = compile_graph(
        graph,
        CompileSnapshot(
            policy_digest=_NULL_POLICY_DIGEST,
            package_digests=admitted_loop_package_digests(
                parse_loop_roots(getattr(namespace, "loop_roots", None))
            ),
            connections=(),
        ),
    )
    assert payload["policy_digest"] == _NULL_POLICY_DIGEST
    assert payload["execution_bound"]["total_attempt_slots"] == total_execution_bound(plan)


@pytest.mark.parametrize("manifest", SHIPPED_GRAPHS, ids=lambda p: p.parent.name)
def test_bound_components_multiply_back_to_the_total(
    manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``(1 + R) * per_round == total`` — the paper's formula, checkable by a reader from our JSON."""
    assert cmd_graph_plan(_ns(manifest, json=True)) == 0
    bound = json.loads(capsys.readouterr().out)["execution_bound"]
    assert (1 + bound["repair_rounds"]) * bound["attempts_per_round"] == bound[
        "total_attempt_slots"
    ]


def test_plan_and_run_agree_on_how_to_resolve_loop_roots() -> None:
    """Both must accept ``--loop-roots``, or plan describes an admission set run will not use."""
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="cmd"))

    for subcommand in ("plan", "run"):
        args = parser.parse_args(["graph", subcommand, "m.yaml", "--loop-roots", "/tmp/loops"])
        assert args.loop_roots == ["/tmp/loops"], f"`bl graph {subcommand}` dropped --loop-roots"


# ── Wave-1 audit round 1: the catalogue is its own failure phase ────────────────────────────────

def test_a_duplicated_loop_package_is_refused_cleanly_not_as_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both auditors found this and it reproduced live: `cp -R` a loop package and plan tracebacks.

    `GraphIntegrityError` is a SIBLING of `GraphValidationError`, not a subclass, so the compile
    handler never covered the admission call that 0.6.8 added ahead of it. `index()` raises when two
    byte-identical packages appear under different names — which is what anyone does before editing
    a variant. Returning 2 at all proves no exception escaped; asserting WHICH message proves the
    catalogue failure is not misreported as a bad manifest, because the user's fix differs.
    """
    source = REPO_ROOT / "loops" / "osv-scanner-example"
    assert (source / "loop.yaml").is_file(), "fixture moved; pick another shipped loop package"
    roots = tmp_path / "loops"
    roots.mkdir()
    shutil.copytree(source, roots / "my-variant")
    shutil.copytree(source, roots / "my-variant-copy")

    rc = cmd_graph_plan(_ns(SHIPPED_GRAPHS[0], loop_roots=[str(roots)]))

    assert rc == 2, "a duplicated package must be refused with an exit code, not an exception"
    err = capsys.readouterr().err
    assert "loop-package catalogue" in err, f"catalogue failure misreported as something else: {err}"
    assert "share digest" in err, "the message should name the actual collision"


def test_a_clean_catalogue_still_plans_so_the_guard_is_not_refusing_everything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Calibration: one copy under one name must admit AND print a bound — rc 0, not rc 2.

    The first version of this test asserted `rc == 2` plus `"compile failed"`, which the UNFIXED
    code also produces, so it discriminated nothing — and its docstring claimed it proved a bound
    was printed while its assertions checked a refusal. Grok caught the mismatch. Declared but not
    enforced, in the test written to enforce a declaration.

    The cause was the fixture, not the assertion: `--loop-roots` REPLACES the default catalogue, so
    a shipped reference graph naming shipped packages can never compile against a foreign root. A
    manifest with no loop nodes can, which is what makes rc 0 the honest expectation here.
    """
    roots = tmp_path / "loops"
    roots.mkdir()
    shutil.copytree(REPO_ROOT / "loops" / "osv-scanner-example", roots / "my-variant")

    rc = cmd_graph_plan(
        _ns(_repair_manifest(tmp_path, repair_budget=1, attempts=1), loop_roots=[str(roots)])
    )

    captured = capsys.readouterr()
    assert rc == 0, f"a clean catalogue must plan, got rc {rc}: {captured.err.strip()}"
    assert "loop-package catalogue" not in captured.err
    assert "bound   : 6 attempt slots max" in captured.out  # (1 + 1) * 3 nodes x 1 attempt


# ── Wave-1 audit round 2: the (1+R) factor, real parity, and catch narrowness ───────────────────

def _repair_manifest(tmp_path: Path, *, repair_budget: int, attempts: int) -> Path:
    """A manifest with R > 0, written as .json because cmd_graph_plan dispatches on the suffix.

    Every shipped reference graph has no repair_budget, so R = 0 for all of them, and
    `(1 + 0) * per_round == total` is an identity that holds however wrong `total` is. Both auditors
    found the bound-components test vacuous for exactly that reason. Nothing but an R > 0 graph
    exercises the factor that distinguishes this bound from the prior work the paper cites.
    """
    def node(nid: str, **extra: object) -> dict[str, object]:
        return {
            "id": nid, "kind": "research_claim", "inputs": {}, "outputs": {"out": "text"},
            "budget": {"max_attempts": attempts, "max_wallclock_s": 60},
            "effects": ["read_only"], "isolation": "workspace_only", **extra,
        }

    verify = node("verify", inputs={"feed": "text"})
    verify["on_failure"] = {"mode": "repair", "target": "fetch"}
    manifest = tmp_path / "repairing.json"
    manifest.write_text(json.dumps({
        "api_version": "bounded-loops.dev/graph/v1",
        "graph_id": "repairing-graph", "version": "1.0.0", "connection_slots": [],
        "nodes": [node("fetch"), node("shape", inputs={"feed": "text"}), verify],
        "edges": [
            {"from_node": "fetch", "from_port": "out", "to_node": "shape", "to_port": "feed",
             "when": None},
            {"from_node": "shape", "from_port": "out", "to_node": "verify", "to_port": "feed",
             "when": None},
        ],
        "policies": {
            "data_class": "public", "fail_mode": "continue_declared",
            "repair_budget": repair_budget,
        },
    }), encoding="utf-8")
    return manifest


def test_the_repair_factor_is_actually_exercised_and_not_an_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With R = 2 and 3 nodes at 1 attempt: 9 slots, 3 per round. The factor must be visible."""
    manifest = _repair_manifest(tmp_path, repair_budget=2, attempts=1)
    assert cmd_graph_plan(_ns(manifest, json=True)) == 0
    bound = json.loads(capsys.readouterr().out)["execution_bound"]

    assert bound["repair_rounds"] == 2
    assert bound["attempts_per_round"] == 3
    assert bound["total_attempt_slots"] == 9
    # The whole point: the components must NOT be equal to the total, or the multiply-back
    # assertion below is the identity that let a wrong total through.
    assert bound["attempts_per_round"] != bound["total_attempt_slots"]
    assert (1 + bound["repair_rounds"]) * bound["attempts_per_round"] == bound["total_attempt_slots"]


def test_plan_uses_loop_roots_rather_than_merely_accepting_the_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The round-1 parity test only checked argparse `dest`; it passed with the wiring deleted.

    This asserts the admission set the command ACTUALLY resolved, which the disclosure line added in
    round 2 makes observable. Reverting `cmd_graph_plan` to `frozenset()` or to a hardcoded
    `parse_loop_roots(None)` changes these numbers, so this test cannot pass without real wiring.
    """
    roots = tmp_path / "loops"
    roots.mkdir()
    shutil.copytree(REPO_ROOT / "loops" / "osv-scanner-example", roots / "only-one")

    assert cmd_graph_plan(_ns(_repair_manifest(tmp_path, repair_budget=1, attempts=1), json=True,
                             loop_roots=[str(roots)])) == 0
    admission = json.loads(capsys.readouterr().out)["admission"]

    assert admission["loop_package_roots"] == [str(roots)], "the flag was parsed but not used"
    expected = admitted_loop_package_digests(parse_loop_roots([str(roots)]))
    assert admission["loop_packages_admitted"] == len(expected) == 1

    # And --loop-roots REPLACES the default catalogue rather than adding to it, which is what both
    # plan's and run's help text denied until this round.
    assert len(expected) < len(admitted_loop_package_digests(None))


def test_phase_one_does_not_swallow_an_unrelated_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calibrates catch NARROWNESS: `except Exception` would pass the other tests but hide bugs.

    Muse's round-2 LOW: asserting only `rc == 2` cannot distinguish a precise handler from a blanket
    one. A ValueError is neither a catalogue inconsistency nor a filesystem error, so it must escape.
    """
    def _boom(_roots: object) -> frozenset[str]:
        raise ValueError("not a catalogue problem")

    # Patch inside loop_node_wiring, because `admitted_digests_or_problem` — the shared resolver
    # both plan and run now use — is what performs the catch. Patching the old cli_graph_inspect
    # symbol silently intercepted nothing after that refactor, and the test failed loudly rather
    # than passing vacuously, which is the behaviour worth having.
    monkeypatch.setattr(
        "bounded_loops.graph.loop_node_wiring.admitted_loop_package_digests", _boom
    )
    with pytest.raises(ValueError, match="not a catalogue problem"):
        cmd_graph_plan(_ns(_repair_manifest(tmp_path, repair_budget=1, attempts=1)))


def test_run_refuses_a_duplicated_package_exactly_as_plan_does(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`bl graph run` tracebacked on the duplicate that plan already exited 2 on.

    Round 2 fixed plan and left run — the third time in one day that a fix reached one call site and
    missed its sibling. Both now go through `admitted_digests_or_problem`, so this test exists to
    fail if they are ever handled separately again. It is the command that SPENDS, so a traceback
    here is worse than on the read-only one.
    """
    roots = tmp_path / "loops"
    roots.mkdir()
    shutil.copytree(REPO_ROOT / "loops" / "osv-scanner-example", roots / "a")
    shutil.copytree(REPO_ROOT / "loops" / "osv-scanner-example", roots / "b")

    rc = cmd_graph_run(_ns(SHIPPED_GRAPHS[0], loop_roots=[str(roots)], execute=False))

    assert rc == 2, "run must refuse with an exit code, not an exception"
    assert "share digest" in capsys.readouterr().err
