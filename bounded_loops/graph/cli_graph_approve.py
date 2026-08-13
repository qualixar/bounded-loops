"""`bl graph approve` handler — split out to keep cli_graph.py within budget.

Records a human decision (approved/rejected) for a paused approval-checkpoint node and
resumes the run past it. This module adds NO new durable-approval logic of its own — it
reuses ``LocalGraphRuntimeFacade.approve()`` (C-080 machinery: authority checks,
idempotent commit, atomic file-locked persistence) unchanged.

Addressing note (0.4.0 — dual-audit reconciliation, design Q4/M2): this command opens
the run directory FLAT — ``--run <dir>`` IS the run root, exactly like ``bl graph
status`` / ``artifacts`` / ``arena`` already treat their own ``--run`` argument. It does
this via ``LocalGraphRuntimeFacade.for_run_dir(run_dir)``, an ADDITIVE facade entry that
opens a literal directory (symlink-guarded, identity loaded from ``run-meta.json`` — see
``graph_runtime_facade.py`` for the full contract) rather than the hosted/multi-tenant
``runs_root/organization_id/project_id/run_id`` convention the original facade
constructor still supports unchanged for deployments that need it. Earlier in 0.4.0-beta
this command instead required a NESTED ``runs_root/org/project/run_id`` layout and
derived ``runs_root`` by climbing three parents off the reported ``--out`` path; both the
Grok and Muse adversarial audits flagged that as MAJOR public-contract debt (it changed
``bl graph run --execute --out <dir>``'s output layout and only existed to satisfy this
command's own path math). That nesting is gone: ``_execute_manifest`` in ``cli_graph.py``
now writes directly into ``<dir>``, and ANY flat run directory — including one built by
calling ``execute_graph_run()`` directly, bypassing the CLI — is addressable here.

Prompts are NOT persisted (C-080): if resuming needs a connector-node prompt re-supplied,
pass ``--inputs`` — the SAME flag ``bl graph run`` uses for the same purpose. ``--inputs``
is refused if it names a symlink, mirroring the guards ``_load_plan_from_run_dir`` already
applies to the run directory's own internal files.

LOCAL TRUST POSTURE — honesty, not a behavior change. This command inherits the same
local-FS trust boundary ``approval_ledger.py`` documents in full; two things are called
out here explicitly because an operator reads THIS docstring, not necessarily the
application layer's:

* ``--decision rejected`` is durably recorded WITHOUT any signature check — only
  ``approved`` is signature-gated (via the ``approvals.approve`` use case's injected
  ``approval_signature_verifier``). A HOSTED deployment must add rejection
  verification (or chain rejections into the hash-chained receipt log) before treating
  a reject as tamper-evident; locally it is not a bug, because the CLI invocation
  itself is the trust boundary.
* Approving a node does NOT require it to currently be ``AWAITING_APPROVAL`` — a
  decision can be durably recorded ahead of the run actually reaching that gate (e.g.
  pre-clearing gate 2 of a two-gate DAG while gate 1 is still pending). Locally this is
  harmless (the decision is simply honored WHEN the run reaches that node); a
  regulated/hosted HITL deployment that needs "the human saw the hold, not just the
  request" must additionally bind the decision to the node's current hold evidence.

Neither of these is claimed as tamper-proof here, and neither should be inferred from
the exit-code/authority machinery working correctly for the LOCAL posture it was built for.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from bounded_loops.graph.application.approval_ledger import _load_approvals
from bounded_loops.graph.application.arena_projection import ArenaProjection, ArenaReadRequest
from bounded_loops.graph.graph_run_report import (
    _EXIT_PAUSED,
    _awaiting_approval_nodes,
    approve_command_hint,
)
from bounded_loops.graph.graph_runtime_facade import LocalGraphRuntimeFacade
from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _load_node_prompts(inputs_path: object) -> tuple[dict[str, str] | None, str | None]:
    """Parse ``--inputs`` into a node_id -> prompt map; (None, message) on failure.

    Refuses a symlinked ``--inputs`` file (fail closed, mirroring the leaf-symlink
    guards ``_load_plan_from_run_dir`` already applies to the run directory's own
    internal files) rather than silently following it to an unrelated target.
    """
    if not inputs_path:
        return {}, None
    path = Path(str(inputs_path))
    if path.is_symlink():
        return None, f"--inputs '{path}' is a symlink; refusing to read it"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"cannot load --inputs — {exc}"
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        return None, "--inputs must be a JSON object mapping node_id -> prompt string"
    return raw, None


def _load_identity_and_facade(
    run_dir: Path, node_prompts: dict[str, str],
) -> tuple[GraphRunIdentity, LocalGraphRuntimeFacade] | tuple[None, None]:
    """Load the run's identity and construct a flat-addressed facade for *run_dir*.

    Returns ``(None, None)`` — with the reason already printed to stderr — if
    *run_dir* is not a valid, safely-openable run directory: missing/corrupt
    ``run-meta.json``, a symlink, or a directory that is not a genuine run at all.
    ``LocalGraphRuntimeFacade.for_run_dir`` (0.4.0 flat addressing) owns every one of
    those checks; this function does not duplicate them.

    Both imports are now module-level (ARCH-01 fix): the cycle that required lazy
    imports here was broken when ``graph_runtime_facade.py`` stopped importing from
    ``cli_graph.py``.
    """
    try:
        facade = LocalGraphRuntimeFacade.for_run_dir(run_dir, node_prompts=node_prompts)
    except (GraphIntegrityError, GraphValidationError) as exc:
        _err(f"graph approve: {exc}")
        return None, None

    # `for_run_dir` already refused a symlinked run_dir and validated it as a genuine
    # run, so resolving here is safe; load identity from the RESOLVED path (not the raw
    # arg) so a `..`/relative path cannot make this second read diverge from the facade's
    # already-validated view (dual-audit convergence MINOR — removes a redundant TOCTOU).
    resolved = run_dir.resolve()
    try:
        _plan, identity, _meta = load_plan_from_run_dir(resolved)
    except (FileNotFoundError, ValueError, GraphValidationError) as exc:
        _err(f"graph approve: cannot reconstruct plan — {exc}")
        return None, None
    return identity, facade


def cmd_graph_approve(args: argparse.Namespace) -> int:
    """bl graph approve --run <dir> --node <id> --decision approved|rejected [--inputs <json>]

    Records a human decision for a paused approval node and resumes the run past it.
    ``--run <dir>`` is opened FLAT (0.4.0) — the exact directory reported by
    ``bl graph run --execute``, no nesting, via ``LocalGraphRuntimeFacade.for_run_dir``.
    """
    run_dir = Path(args.run)
    if not run_dir.is_dir():
        _err(f"graph approve: '{run_dir}' is not a directory")
        return 2

    node_prompts, error = _load_node_prompts(getattr(args, "inputs", None))
    if error is not None:
        _err(f"graph approve: {error}")
        return 2

    identity, facade = _load_identity_and_facade(run_dir, node_prompts or {})
    if identity is None or facade is None:
        return 2

    request = ArenaReadRequest(
        subject_id=identity.organization_id,
        organization_id=identity.organization_id,
        project_id=identity.project_id,
        run_id=identity.run_id,
    )

    # DX-06: detect idempotent re-approval BEFORE calling approve so we can emit the
    # "(already recorded — idempotent)" note.  Read-only ledger check; fail-open so a
    # missing/unreadable ledger never blocks the approve command itself.
    already_decided = _already_decided(run_dir.resolve(), args.node)

    try:
        # A run that paused on its spend ceiling needs one supplied here too: approving a
        # checkpoint CONTINUES the run, and the controller refuses to continue a paused run with
        # no ceiling. Without these flags a budget pause followed by a human gate could not be
        # finished from this command at all.
        from bounded_loops.graph.cli_graph import _resolve_budget

        run_budget, price_table = _resolve_budget(args)
        projection = facade.approve(
            request, node_id=args.node, decision=args.decision,
            run_budget=run_budget if run_budget.declared else None,
            price_table=price_table if price_table.prices else None,
        )
    except (GraphIntegrityError, GraphValidationError) as exc:
        _err(f"graph approve: {exc}")
        return 2

    return _report_approve(args, run_dir=run_dir, projection=projection, already_decided=already_decided)


def _already_decided(run_dir: Path, node_id: str) -> bool:
    """Return True if *node_id* already has a durable decision in the approval ledger.

    Fail-open (returns False on any I/O or integrity error) so this read-only probe
    never blocks the approve command from proceeding.
    """
    try:
        ledger = _load_approvals(run_dir / "approvals.json")
        commits = ledger.get("commits", [])
        rejections = ledger.get("rejections", [])
        return any(c.get("node_id") == node_id for c in commits) or any(
            r.get("node_id") == node_id for r in rejections
        )
    except Exception:  # noqa: BLE001
        return False


def _report_approve(
    args: argparse.Namespace,
    *,
    run_dir: Path,
    projection: ArenaProjection,
    already_decided: bool = False,
) -> int:
    awaiting = _awaiting_approval_nodes(projection)
    succeeded = projection.run_state == "SUCCEEDED"
    # A run whose authoritative run_state is FAILED must never report as merely
    # PAUSED, even if a node's last durable receipt still shows AWAITING_APPROVAL —
    # mirrors execute_graph._report's same fix (dual-audit residual MINOR). PAUSED
    # implies the run is still resumable; a FAILED run is not.
    failed = projection.run_state == "FAILED"
    still_paused = bool(awaiting) and not failed
    next_commands = [approve_command_hint(run_dir, node_id) for node_id in awaiting]

    if getattr(args, "json", False):
        print(json.dumps({
            "run_state": projection.run_state,
            "run_id": projection.run_id,
            "out": str(run_dir),
            "node_id": args.node,
            "decision": args.decision,
            "idempotent": already_decided,
            "paused": still_paused,
            "awaiting_approval": list(awaiting),
            "next_commands": next_commands,
        }, sort_keys=True))
        if still_paused:
            return _EXIT_PAUSED
        return 0 if succeeded else 2

    print(f"node {args.node!r} decision : {args.decision}")
    if already_decided:
        print("  (already recorded — idempotent)")
    print(f"run_state : {projection.run_state}")
    for node in projection.nodes:
        print(f"  node {node.node_id!r}: {node.state}")
    if still_paused:
        print()
        print(f"Run is still PAUSED — awaiting approval on: {', '.join(awaiting)}")
        for command in next_commands:
            print(f"  {command}")
        return _EXIT_PAUSED
    if succeeded:
        print()
        print(f"Open the visual Arena:  bl graph arena --run {run_dir}")
        return 0
    print()
    print("Run did not succeed; inspect the event log in the run directory.")
    return 2
