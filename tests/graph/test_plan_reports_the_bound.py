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
from pathlib import Path

import pytest

from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.repair_rounds import total_execution_bound
from bounded_loops.graph.application.validate_graph import parse_authoring_graph_yaml
from bounded_loops.graph.cli_graph import cmd_graph_plan, register
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
    assert "bound   :" in captured.out, f"no bound reported for {manifest.parent.name}"


@pytest.mark.parametrize("manifest", SHIPPED_GRAPHS, ids=lambda p: p.parent.name)
def test_printed_bound_is_the_authority_not_a_recomputation(
    manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The number on screen must be ``total_execution_bound``'s, or display and bound can drift."""
    assert cmd_graph_plan(_ns(manifest, json=True)) == 0
    payload = json.loads(capsys.readouterr().out)

    # Recompile the same way the command does, then compare against the authority directly.
    graph = parse_authoring_graph_yaml(manifest.read_text(encoding="utf-8"))
    plan = compile_graph(
        graph,
        CompileSnapshot(
            policy_digest=_NULL_POLICY_DIGEST,
            package_digests=admitted_loop_package_digests(parse_loop_roots(None)),
            connections=(),
        ),
    )
    assert payload["policy_digest"] == _NULL_POLICY_DIGEST
    assert payload["execution_bound"]["total_node_executions"] == total_execution_bound(plan)


@pytest.mark.parametrize("manifest", SHIPPED_GRAPHS, ids=lambda p: p.parent.name)
def test_bound_components_multiply_back_to_the_total(
    manifest: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``(1 + R) * per_round == total`` — the paper's formula, checkable by a reader from our JSON."""
    assert cmd_graph_plan(_ns(manifest, json=True)) == 0
    bound = json.loads(capsys.readouterr().out)["execution_bound"]
    assert (1 + bound["repair_rounds"]) * bound["attempts_per_round"] == bound[
        "total_node_executions"
    ]


def test_plan_and_run_agree_on_how_to_resolve_loop_roots() -> None:
    """Both must accept ``--loop-roots``, or plan describes an admission set run will not use."""
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="cmd"))

    for subcommand in ("plan", "run"):
        args = parser.parse_args(["graph", subcommand, "m.yaml", "--loop-roots", "/tmp/loops"])
        assert args.loop_roots == ["/tmp/loops"], f"`bl graph {subcommand}` dropped --loop-roots"
