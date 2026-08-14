"""Application-layer helpers for reconstructing a run's plan from a persisted directory.

This module holds the logic that was previously embedded in ``cli_graph.py`` as the
private function ``_load_plan_from_run_dir``.  It belongs in the **application** layer
because it uses only application- and domain-level types (``compile_graph``,
``ExecutionPlan``, ``GraphRunIdentity``, ``GraphValidationError``) and performs no
CLI-specific I/O.  Moving it here breaks the ``application → cli_graph → application``
import cycle that existed in 0.4.0-pre (ARCH-01 in the architecture audit).

Both callers — ``LocalGraphRuntimeFacade`` and the CLI handlers — now import from here.
The console server's lazy-import workaround comment in ``server.py`` is no longer needed
and has been removed.
"""

from __future__ import annotations

import json
from pathlib import Path

from bounded_loops.graph.application.compile_graph import CompileSnapshot, compile_graph
from bounded_loops.graph.application.validate_graph import parse_authoring_graph_yaml
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan


def _mismatch_explanation(meta: dict[str, object], current_compiler: str) -> str:
    """Name the likely cause of a plan_id mismatch. Returns ``""`` when there is nothing to add.

    A tampered run directory and an engine upgrade produce the SAME symptom — a recompile that does
    not match the stored id — and the bare two-digest message pointed at the first, so a user whose
    only "change" was upgrading went looking for an edit that did not exist. That is what made the
    0.4.0 publish-node break silent (P4.5 audit, Grok 8): the compiler started carrying
    ``publication_policy`` in the plan, every publish graph's id moved, and the error blamed the
    directory. The digest change itself is fixed in ``compile_graph``; this makes the NEXT one
    diagnosable, because a compiler change that moves plan_id is always possible.
    """
    recorded = meta.get("compiler_version")
    if not isinstance(recorded, str) or not recorded:
        return (
            "\nThis run directory records no compiler_version (written by 0.4.0 or earlier), so the "
            "cause cannot be narrowed automatically: either this engine's compiler produces a "
            "different plan for the same manifest, or the directory was modified."
        )
    if recorded != current_compiler:
        return (
            f"\nThe run was compiled by {recorded!r} and this engine compiles {current_compiler!r}. "
            "A compiler change is the likely cause, NOT a modified run directory. Resume the run "
            "with the engine version that created it, or start a fresh run from the same manifest."
        )
    return (
        f"\nBoth this engine and the run record compiler {recorded!r}, so the manifest, connections "
        "or policy digest in this directory no longer produce the plan it was created with."
    )


def load_plan_from_run_dir(
    run_dir: Path,
    *,
    package_digests: frozenset[str] = frozenset(),
) -> tuple[ExecutionPlan, GraphRunIdentity, dict[str, object]]:
    """Reconstruct plan + identity + raw meta from a persisted run directory.

    Performs symlink guards on the run directory and each critical internal file
    (TOCTOU mitigation) before reading any content.  Raises on any structural
    problem so every caller gets a typed failure they can catch cleanly:

    * ``FileNotFoundError`` — ``run-meta.json`` is absent.
    * ``ValueError`` — JSON is malformed or a required key is missing, or the
      recompiled plan_id does not match the stored one.
    * ``GraphValidationError`` — the stored manifest fails authoring validation or
      compilation (connection bindings do not satisfy the graph's slots).

    The caller is responsible for resolving ``run_dir`` to an absolute path before
    calling this function (e.g. via ``run_dir.resolve()``) so that symlink detection
    on the directory itself is meaningful.
    """
    # Symlink guards — reject TOCTOU-capable paths on the run dir and internal files.
    if run_dir.is_symlink():
        raise ValueError(f"run directory '{run_dir}' is a symlink; aborting")
    for _n in ("run-meta.json", "manifest.yaml", "connections.json", "controller-events.jsonl"):
        if (run_dir / _n).is_symlink():
            raise ValueError(f"internal file '{_n}' is a symlink; aborting")

    meta_path = run_dir / "run-meta.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"run-meta.json not found in {run_dir}")
    try:
        meta: dict[str, object] = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"run-meta.json is not valid JSON: {exc}") from exc
    try:
        policy_digest = str(meta["policy_digest"])
        stored_plan_id = str(meta["plan_id"])
        org_id = str(meta["organization_id"])
        proj_id = str(meta["project_id"])
        run_id_ = str(meta["run_id"])
    except KeyError as exc:
        raise ValueError(f"run-meta.json is missing required key {exc}") from exc

    manifest_text = (run_dir / "manifest.yaml").read_text(encoding="utf-8")
    graph = parse_authoring_graph_yaml(manifest_text)
    raw_connections = json.loads(
        (run_dir / "connections.json").read_text(encoding="utf-8")
    )
    snapshot = CompileSnapshot(
        policy_digest=policy_digest,
        package_digests=package_digests,
        connections=tuple(raw_connections),  # type: ignore[arg-type]
    )
    plan = compile_graph(graph, snapshot)

    if plan.plan_id != stored_plan_id:
        raise ValueError(
            f"Reconstructed plan_id {plan.plan_id!r} != stored {stored_plan_id!r}"
            f"{_mismatch_explanation(meta, plan.compiler_version)}"
        )
    identity = GraphRunIdentity(
        organization_id=org_id,
        project_id=proj_id,
        run_id=run_id_,
        graph_digest=plan.source_graph_digest,
        plan_digest=plan.plan_id,
        policy_digest=plan.policy_digest,
    )
    # The fail mode decides whether a continuation drives the graph past a node failure, so it must
    # come from a source an edit cannot forge. ``manifest.yaml`` is covered by the graph digest —
    # which the plan_id check above has just verified — while ``run-meta.json`` is unsigned JSON that
    # anyone with write access to the run directory can change. So the manifest wins, and a
    # disagreement is treated as tampering rather than quietly preferred either way.
    #
    # Found by the P4.25a dual audit (Muse finding 2): reading the mode from run-meta let a
    # filesystem edit flip a fail_closed run into one that continues past gate rejections, with the
    # plan_id check still passing — because the check recompiles from manifest.yaml and never reads
    # run-meta.json at all, so nothing in run-meta is covered by any digest.
    #
    # An earlier version of this comment said the check passed "because fail_mode is deliberately not
    # in the plan's canonical form". That was wrong, and the P4.5 audit caught it: fail_mode IS in
    # `_canonical_policies` → `_canonical_graph` → `graph.digest` → `source_graph_digest`, which sits
    # in `_canonical_plan`. So an edit to the MANIFEST's fail_mode does move plan_id. The unsigned
    # file was always the whole problem, and the comment named the wrong reason for the right fix.
    authored_fail_mode = graph.policies.fail_mode
    recorded = meta.get("fail_mode")
    if isinstance(recorded, str) and recorded and recorded != authored_fail_mode:
        raise ValueError(
            f"run-meta.json fail_mode {recorded!r} disagrees with the manifest's "
            f"{authored_fail_mode!r}; the run directory has been modified"
        )
    meta["fail_mode"] = authored_fail_mode
    return plan, identity, meta
