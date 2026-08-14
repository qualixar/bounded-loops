"""Write the run directory a later ``status`` / ``arena`` / ``resume`` reconstructs a run from.

The symmetric half of ``plan_persistence``, which reads one back. Extracted from the composition
root when that module crossed the 800-line cap; it belongs in the application layer for the same
reason the loader does — it uses only application and domain types and performs no CLI-specific I/O.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Sequence

from bounded_loops.graph.application.failure_policy import HALT_AT_FIRST_FAILURE
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan


def persist_run_dir(
    out_dir: Path,
    plan: ExecutionPlan,
    manifest_text: str,
    connections_raw: Sequence[object],
    identity: GraphRunIdentity,
    *,
    mode: str = "local_cli",
    audit_plan_json: str | None = None,
    provider_catalog: Path | None = None,
    fail_mode: str = HALT_AT_FIRST_FAILURE,
) -> None:
    """Persist the four files ``bl graph status`` / ``arena`` reconstruct any run from. Written
    after the run so a crash never leaves a half-written receipt claiming success. Run-time inputs
    (prompts) are deliberately NOT persisted — a prompt may carry a secret, and the content-addressed
    reply artifact is the durable receipt; the portable graph reconstructs from manifest+connections.

    When ``audit_plan_json`` is supplied it is written verbatim as ``audit-plan.json`` in the run
    directory so ``bl graph arena`` can later compute the cross-model audit coverage projection.
    The controller loop is NOT involved in this persistence — it is a read-side concern only.
    """
    (out_dir / "plan.json").write_bytes(plan.canonical_json)
    (out_dir / "manifest.yaml").write_text(manifest_text, encoding="utf-8")
    (out_dir / "connections.json").write_text(
        json.dumps(list(connections_raw), sort_keys=True), encoding="utf-8"
    )
    run_meta = {
        "execution": True,
        "mode": mode,
        "organization_id": identity.organization_id,
        "plan_id": plan.plan_id,
        "policy_digest": plan.policy_digest,
        "project_id": identity.project_id,
        "run_id": identity.run_id,
        "platform": sys.platform,
        # Recorded so a resume/approve continuation drives the graph the same way the original run
        # did. Not on ExecutionPlan: the plan's canonical_json feeds plan_id, so a field there would
        # change every digest and make existing run directories unresumable.
        "fail_mode": fail_mode,
        # Recorded so a plan_id mismatch on resume can be EXPLAINED rather than blamed on the run
        # directory. A compiler change and a tampered directory produce the identical symptom — a
        # recompile that does not match the stored id — and the old error reported only the two
        # digests, which sent a user hunting for an edit that never happened. A run written by 0.4.0
        # has no such key, and the loader says so instead of guessing (P4.5 audit, Grok 8).
        "compiler_version": plan.compiler_version,
    }
    if provider_catalog is not None:
        # Recorded so every CONTINUE path resolves the same provider map this run used. Without it,
        # a catalog that overrode a shipped name (an operator pointing ``claude`` at their own
        # wrapper) was silently dropped on resume/approve, and the continuation invoked — and paid
        # for — the shipped binary instead. The digest turns a catalog EDITED between the run and the
        # resume from an invisible difference into a warning.
        # Absolute: a relative string resolves against the CWD of whichever process reads it
        # later, so a resume from another directory opened a different file or none.
        run_meta["provider_catalog"] = str(provider_catalog.resolve())
        try:
            run_meta["provider_catalog_sha256"] = hashlib.sha256(
                provider_catalog.read_bytes()
            ).hexdigest()
        except OSError:
            run_meta["provider_catalog_sha256"] = ""
    (out_dir / "run-meta.json").write_text(json.dumps(run_meta, sort_keys=True), encoding="utf-8")
    if audit_plan_json is not None:
        (out_dir / "audit-plan.json").write_text(audit_plan_json, encoding="utf-8")


# A paused run is neither a success (0) nor a failure (2) — it is durably waiting on a
# human decision, and the CLI must never let a caller (or a CI script checking for a
# non-zero exit) mistake "paused" for "broken". Exit code 3 is otherwise unused across
# the graph CLI (0/1/2 already carry meaning: 0=success, 1=CLI usage error, 2=refused
# or failed run) so it cannot collide with an existing convention.
