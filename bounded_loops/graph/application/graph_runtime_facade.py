"""Real ``LocalGraphRuntimeFacade`` — status / resume / approve over a persisted run directory.

This is the deployment-side facade that the MCP shim (``mcp_graph.register``) injects.
It wires real arena reads, real connector workers, and the real ``approvals.approve`` use case
through a narrow set of file-based local adapters.

Security model
--------------
* Authorization is enforced by ``SameTenantArenaAuthorizer`` (subject == org, same-tenant only).
* Prompts are NOT persisted — they are supplied fresh on every resume call.  If a connector node
  needs a prompt and none is supplied, ``resume`` FAILS CLOSED with a clear message.
* Approval authority flows from the ``approvals.approve`` use case. The authorizer + signature
  verifier are INJECTABLE (``approval_authorizer`` / ``approval_signature_verifier``): a hosted
  deployment supplies a real crypto verifier + a role-checking authorizer. The LOCAL defaults
  authorize any same-tenant subject and accept a non-empty signature because the MCP session IS the
  authentication boundary for local runs — they do NOT verify the actor's role (that is the hosted
  authorizer's job).
* The ``_FileApprovalCommandPort`` persists decisions atomically (``os.replace``) with an
  exclusive file lock so concurrent resume+approve calls cannot corrupt the decision record.

Run directory layout (``runs_root / org / project / run_id /``)
----------------------------------------------------------------
  plan.json                — canonical execution plan bytes (written by execute_graph_run)
  manifest.yaml            — original authoring manifest
  connections.json         — admitted connection records (JSON array)
  run-meta.json            — execution metadata (plan_id, org, project, run_id, policy_digest)
  controller-events.jsonl  — hash-chained event log
  approvals.json           — (written here) durable approval decision records
  artifacts/               — per-node artifact store

Two addressing modes (0.4.0 — dual-audit reconciliation, design Q4/M2)
-----------------------------------------------------------------------
* ``LocalGraphRuntimeFacade(runs_root=..., arena_authorizer=...)`` — the ORIGINAL
  hosted/multi-tenant mode above: every run lives at ``runs_root/org/project/run_id``,
  and every segment is validated + containment-checked against ``runs_root`` before use
  (``_run_dir`` / ``_safe_segment``). Unchanged; still the right mode for a deployment
  that serves many tenants out of one root.
* ``LocalGraphRuntimeFacade.for_run_dir(run_dir, ...)`` — ADDITIVE. Opens ONE run
  directory LITERALLY: no org/project/run_id join, because there is no join — the
  caller's own path IS the run root (the same contract ``bl graph status`` / ``arena`` /
  ``artifacts`` already give their own ``--run <dir>``). It reuses
  ``cli_graph._load_plan_from_run_dir`` for the SAME symlink guards and identity
  reconstruction those commands already trust, so opening a flat run this way is no
  weaker than the existing traversal discipline — there is simply nothing left to
  traverse. ``bl graph run --execute --out <dir>`` writes flat (directly into ``<dir>``)
  as of 0.4.0, and ``bl graph approve`` uses this classmethod to open it.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from bounded_loops.graph.adapters.connectors.admitted_connection_request import AdmittedConnectionRecord
from bounded_loops.graph.adapters.connectors.local_cli_worker import CLI_PROFILES, CliProfile
from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.application.approval_ledger import (
    _approval_id,
    _load_approvals,
    _rehydrated_request,
    build_durable_approval_resolver,
)
from bounded_loops.graph.application.approvals import (
    ApprovalAuthorizationPort,
    ApprovalCommand,
    ApprovalCommit,
    ApprovalSignatureVerifierPort,
    ApprovalTarget,
    AuthenticatedApprovalContext,
    approve as _approve_use_case,
    request_digest as _request_digest,
)
from bounded_loops.graph.application.arena_projection import (
    ArenaAuthorizationPort,
    ArenaProjection,
    ArenaReadRequest,
    latest_node_states,
    read_arena_projection,
)
from bounded_loops.graph.application.egress_broker import EgressBroker
from bounded_loops.graph.application.execute_graph import (
    build_execution_controller,
)
from bounded_loops.graph.application.run_graph import is_egress_node
from bounded_loops.graph.cli_graph import _load_plan_from_run_dir
from bounded_loops.graph.domain.approvals import ApprovalDecision, ApprovalRequest
from bounded_loops.graph.domain.authoring import NodeKind
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity, StoredGraphEvent
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

# _approval_id, _load_approvals, and _rehydrated_request are RE-EXPORTED here (not just
# used internally) because existing tests import them from this module path — see
# tests/graph/application/test_graph_runtime_facade_security.py and
# test_graph_runtime_facade.py::test_load_approvals_rejects_non_list_commits. The single
# real implementation now lives in approval_ledger.py, shared with execute_graph.py.

_ALL_EXECUTOR_TRANSPORTS = frozenset({"local_cli", "https"})

# A run-dir path segment: no "/", no "..", no absolute — reject a traversal before it is joined.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _safe_segment(value: str, name: str) -> str:
    if not isinstance(value, str) or value in (".", "..") or _SAFE_SEGMENT.fullmatch(value) is None:
        raise GraphIntegrityError(f"runtime facade: unsafe {name!r} path segment")
    return value


# ── public authorizer ─────────────────────────────────────────────────────────


class SameTenantArenaAuthorizer:
    """Allow reads only when the subject IS the organization (local same-tenant runs).

    A subject that differs from the organization is rejected; cross-tenant access is
    further blocked by ``read_arena_projection``'s tenant-match check (which compares
    the request's org/project/run against the event-log identity BEFORE calling the
    authorizer).
    """

    def authorize(self, request: ArenaReadRequest) -> bool:
        return request.subject_id == request.organization_id


# ── private arena receipt verifier ────────────────────────────────────────────


class _NoopArenaReceiptVerifier:
    """No-op verifier for locally produced receipts (hash-chain integrity is enough)."""

    def verify(self, identity: GraphRunIdentity, receipts: tuple[StoredGraphEvent, ...]) -> None:
        return None


# ── private approval port adapters ────────────────────────────────────────────


class _SameTenantApprovalAuthorizer:
    """Permit approval only when the authenticated subject is within the same tenant."""

    def authorize(self, request: ApprovalRequest, context: AuthenticatedApprovalContext) -> bool:
        return (
            bool(context.subject_id)
            and context.organization_id == request.organization_id
            and context.project_id == request.project_id
        )


class _LocalApprovalSignatureVerifier:
    """Accept any non-empty signature for local MCP-authenticated runs.

    The MCP session IS the authentication boundary; external cryptographic signatures are
    not required for same-host runs where the subject is trusted by the MCP transport.
    """

    def verify(self, request: ApprovalRequest, decision: ApprovalDecision) -> bool:
        return isinstance(decision.signature, str) and bool(decision.signature.strip())


@dataclass
class _FileApprovalCommandPort:
    """File-backed durable approval persistence.

    Persists decisions to ``run_dir / "approvals.json"`` with an exclusive ``fcntl.flock``
    and ``os.replace`` atomic write.  Idempotency is enforced: re-submitting the same
    ``idempotency_key`` returns the original commit without mutating the record.
    """

    _run_dir: Path

    def commit(self, command: ApprovalCommand) -> ApprovalCommit:
        approval_path = self._run_dir / "approvals.json"
        lock_path = self._run_dir / "approvals.lock"
        lock_path.touch(exist_ok=True)

        with lock_path.open("r+") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                record = _load_approvals(approval_path)
                # Fail closed on the MIRROR conflict: never approve a node that already carries a durable
                # rejection (commit_rejection enforces the reverse). This holds the "never both" invariant
                # at the PORT level even under concurrency — not only via the facade's serial pre-check
                # (re-audit N1: a concurrent approve racing a rejection would otherwise slip through).
                if any(r.get("node_id") == command.request.node_id for r in record.get("rejections", [])):
                    raise GraphIntegrityError(
                        f"cannot approve node {command.request.node_id!r}: a durable rejection already exists for it"
                    )
                # Idempotency: same key → same commit
                for stored in record.get("commits", []):
                    if stored.get("idempotency_key") == command.idempotency_key:
                        return ApprovalCommit(
                            approval_id=stored["approval_id"],
                            new_resource_version=stored["new_resource_version"],
                            idempotency_key=stored["idempotency_key"],
                        )
                # Resource-version guard (already validated by use case, but defensive)
                current_version = record.get("resource_version", 1)
                if current_version != command.expected_resource_version:
                    raise GraphValidationError(
                        "approval_stale",
                        "/expected_resource_version",
                        f"approval version mismatch: expected {command.expected_resource_version}, "
                        f"got {current_version}",
                    )
                new_version = current_version + 1
                commit = ApprovalCommit(
                    approval_id=command.request.approval_id,
                    new_resource_version=new_version,
                    idempotency_key=command.idempotency_key,
                )
                commits = list(record.get("commits", []))
                commits.append({
                    "approval_id": commit.approval_id,
                    "new_resource_version": commit.new_resource_version,
                    "idempotency_key": commit.idempotency_key,
                    "node_id": command.request.node_id,
                    "actor_id": command.context.subject_id,
                    "decided_at": command.decision.decided_at,
                })
                # Write an ALLOW-LISTED schema only: preserve the durable ``rejections`` list (so an
                # approval commit never wipes a prior rejection) but never re-serialize unknown/hostile
                # keys, which would let junk accumulate and bloat every future write (dual-audit MAJOR).
                new_record = {
                    "resource_version": new_version,
                    "commits": commits,
                    "rejections": list(record.get("rejections", [])),
                }
                _atomic_write(approval_path, json.dumps(new_record, indent=2).encode("utf-8"))
                return commit
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def commit_rejection(
        self, *, node_id: str, attempt: int, approval_id: str, actor_id: str, decided_at: str,
    ) -> None:
        """Durably persist a human REJECTION so a later resume re-honors it (C-078 follow-up).

        Rejections do not flow through the ``approvals.approve`` use case (which only GRANTS); they
        are recorded here under the SAME exclusive ``flock`` + ``os.replace`` atomic-write discipline,
        in a separate ``rejections`` list so the approval version chain is untouched. Idempotent by
        ``(node_id, attempt)``. The ``approval_id`` (the deterministic run+node id) is stored so the
        rehydration path can reject a foreign rejection record exactly as it does for approvals —
        rejections must not be a weaker, unguarded forgery/DoS surface (dual-audit MAJOR).

        FAIL-CLOSED on conflict: refuses to reject a node that already carries a durable APPROVAL
        (and the approval path refuses the mirror case), so the ledger can never hold both decisions
        for one node."""
        approval_path = self._run_dir / "approvals.json"
        lock_path = self._run_dir / "approvals.lock"
        lock_path.touch(exist_ok=True)
        with lock_path.open("r+") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                record = _load_approvals(approval_path)
                if any(c.get("node_id") == node_id for c in record.get("commits", [])):
                    raise GraphIntegrityError(
                        f"cannot reject node {node_id!r}: a durable approval already exists for it"
                    )
                rejections = list(record.get("rejections", []))
                for stored in rejections:
                    if stored.get("node_id") == node_id and int(stored.get("attempt", 1)) == attempt:
                        return  # already rejected — idempotent
                rejections.append({
                    "node_id": node_id, "attempt": attempt, "approval_id": approval_id,
                    "actor_id": actor_id, "decided_at": decided_at,
                })
                new_record = {
                    "resource_version": record.get("resource_version", 1),
                    "commits": list(record.get("commits", [])),
                    "rejections": rejections,
                }
                _atomic_write(approval_path, json.dumps(new_record, indent=2).encode("utf-8"))
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(str(tmp), str(path))


# ── facade ────────────────────────────────────────────────────────────────────


@dataclass
class LocalGraphRuntimeFacade:
    """Concrete ``GraphRuntimeFacade`` for local run directories.

    Injects all real ports; the MCP shim sees only the three protocol methods.

    Parameters
    ----------
    runs_root:
        Root directory under which run dirs live as ``org / project / run_id /``.
    arena_authorizer:
        Authorization port for ``read_arena_projection``.  Pass ``SameTenantArenaAuthorizer``
        for same-tenant local access.
    cli_profiles:
        Mapping of profile name → ``CliProfile`` forwarded to ``build_execution_controller``.
    environ:
        Environment overrides forwarded to workers.
    node_prompts:
        Re-supplied prompts for connector nodes that need to be re-driven on resume.
        NOT persisted — callers must re-supply on every resume / approve call.
    admitted_connections:
        BYOK/https admitted-connection records.  Pass ``None`` (default) for local-CLI-only runs.
    byok_egress_broker / byok_credential_resolver / byok_tls_context:
        Injectable BYOK infrastructure passed through to ``build_execution_controller``.

    Do not set ``_literal_run_dir`` directly — construct via ``for_run_dir`` instead,
    which validates the directory (symlink guard + identity load) before this field is
    ever populated. When it is set, ``runs_root`` is inert (present only so the field
    stays required/typed for the original mode) and every run-directory lookup returns
    ``_literal_run_dir`` unchanged — see ``_run_dir``.
    """

    runs_root: Path
    arena_authorizer: ArenaAuthorizationPort
    cli_profiles: Mapping[str, CliProfile] = field(default_factory=lambda: dict(CLI_PROFILES))
    environ: Mapping[str, str] | None = None
    node_prompts: Mapping[str, str] = field(default_factory=dict)
    admitted_connections: Mapping[str, AdmittedConnectionRecord] | None = None
    byok_egress_broker: EgressBroker | None = None
    byok_credential_resolver: object = None
    byok_tls_context: ssl.SSLContext | None = None
    # Approval authority is INJECTABLE so a hosted deployment supplies a real crypto signature
    # verifier + a role-checking authorizer; the local defaults authorize any same-tenant subject
    # and accept the MCP session as the authentication boundary (documented local posture).
    approval_authorizer: ApprovalAuthorizationPort | None = None
    approval_signature_verifier: ApprovalSignatureVerifierPort | None = None
    # ADDITIVE flat-addressing mode (0.4.0 dual-audit reconciliation, design Q4/M2) — set
    # ONLY by `for_run_dir`. `kw_only=True` (Python 3.10+ per-field option) slots this in
    # WITHOUT disturbing the required-field ordering of `runs_root`/`arena_authorizer`
    # above: existing callers' constructor calls are byte-for-byte unaffected.
    _literal_run_dir: Path | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        # `_literal_run_dir` is a FACTORY-ONLY field: `for_run_dir` sets it to a
        # validated absolute, resolved, non-symlink run directory. A caller that sets
        # it directly on the constructor would bypass those checks, so re-assert the
        # invariant here as defense in depth (dual-audit convergence MINOR).
        if self._literal_run_dir is not None and (
            not self._literal_run_dir.is_absolute() or self._literal_run_dir.is_symlink()
        ):
            raise GraphIntegrityError(
                "_literal_run_dir must be an absolute, non-symlink path set via "
                "LocalGraphRuntimeFacade.for_run_dir()"
            )

    # ── additive constructor: open ONE run directory literally ──────────────

    @classmethod
    def for_run_dir(
        cls,
        run_dir: Path,
        *,
        arena_authorizer: ArenaAuthorizationPort | None = None,
        cli_profiles: Mapping[str, CliProfile] | None = None,
        environ: Mapping[str, str] | None = None,
        node_prompts: Mapping[str, str] | None = None,
        admitted_connections: Mapping[str, AdmittedConnectionRecord] | None = None,
        byok_egress_broker: EgressBroker | None = None,
        byok_credential_resolver: object = None,
        byok_tls_context: ssl.SSLContext | None = None,
        approval_authorizer: ApprovalAuthorizationPort | None = None,
        approval_signature_verifier: ApprovalSignatureVerifierPort | None = None,
    ) -> "LocalGraphRuntimeFacade":
        """Address a run by its LITERAL directory — no org/project/run_id join.

        ``run_dir`` IS the run root: it must directly contain ``run-meta.json``,
        ``manifest.yaml``, ``connections.json``, ``controller-events.jsonl`` — exactly
        what ``bl graph run --execute --out <dir>`` writes as of 0.4.0 (flat, no
        nesting). Validated fail-closed BEFORE the facade is constructed, not lazily on
        first use:

        1. ``run_dir`` itself must not be a symlink (checked on the path as GIVEN,
           before ``resolve()`` — a symlink leaf is refused regardless of its target,
           the same TOCTOU discipline ``_load_plan_from_run_dir`` already applies for
           ``bl graph status`` / ``artifacts`` / ``arena``).
        2. The resolved path must exist and be a directory.
        3. ``cli_graph._load_plan_from_run_dir`` must be able to reconstruct an
           identity from it — a missing/corrupt ``run-meta.json``, a manifest that
           will not recompile, or a stored ``plan_id`` that does not match the
           recompiled plan are all refused here (a directory that merely EXISTS is not
           a run).

        Every failure mode raises ``GraphIntegrityError`` — one exception type for
        every "this is not a safely-openable run directory" case, so callers need only
        one ``except`` clause.
        """
        if run_dir.is_symlink():
            raise GraphIntegrityError(f"run directory '{run_dir}' is a symlink; refusing to open it")
        resolved = run_dir.resolve()
        if not resolved.is_dir():
            raise GraphIntegrityError(
                f"run directory '{run_dir}' does not exist or is not a directory"
            )
        try:
            _load_plan_from_run_dir(resolved)
        except FileNotFoundError as exc:
            raise GraphIntegrityError(f"'{run_dir}' is not a run directory: {exc}") from exc
        except (ValueError, OSError, GraphValidationError) as exc:
            raise GraphIntegrityError(f"'{run_dir}' is not a valid run directory: {exc}") from exc
        return cls(
            runs_root=resolved,  # inert in this mode — every lookup returns _literal_run_dir
            arena_authorizer=arena_authorizer or SameTenantArenaAuthorizer(),
            cli_profiles=cli_profiles if cli_profiles is not None else dict(CLI_PROFILES),
            environ=environ,
            node_prompts=node_prompts or {},
            admitted_connections=admitted_connections,
            byok_egress_broker=byok_egress_broker,
            byok_credential_resolver=byok_credential_resolver,
            byok_tls_context=byok_tls_context,
            approval_authorizer=approval_authorizer,
            approval_signature_verifier=approval_signature_verifier,
            _literal_run_dir=resolved,
        )

    # ── GraphRuntimeFacade protocol ──────────────────────────────────────────

    def status(self, request: ArenaReadRequest) -> ArenaProjection:
        """Read the current arena projection for an existing run (side-effect-free)."""
        plan, identity, _meta = self._load(request)
        run_dir = self._run_dir(request)
        event_log = GraphEventLog(run_dir / "controller-events.jsonl", identity)
        return read_arena_projection(
            plan, event_log, request,
            self.arena_authorizer, _NoopArenaReceiptVerifier(),
        )

    def resume(self, request: ArenaReadRequest) -> ArenaProjection:
        """Resume an interrupted run, returning the post-resume projection.

        FAILS CLOSED if any connector node is non-terminal (not SUCCEEDED/FAILED) and its
        node_id is absent from ``node_prompts`` — prompts are not persisted, so re-supply
        them on every resume call.

        Resuming an already-terminal run is idempotent: the projection is returned unchanged.
        """
        plan, identity, _meta = self._load(request)
        self._authorize_mutation(request, identity)
        run_dir = self._run_dir(request)
        event_log = GraphEventLog(run_dir / "controller-events.jsonl", identity)
        self._check_connector_prompts(plan, event_log)
        # Re-honor any human decision that was durably committed BEFORE the crash (approve-then-crash /
        # reject-then-crash edge): a bare resume must not re-pause a gate a human already decided.
        resolver = build_durable_approval_resolver(identity=identity, plan=plan, run_dir=run_dir)
        try:
            controller, _store, event_log = build_execution_controller(
                plan=plan,
                identity=identity,
                out_dir=run_dir,
                node_prompts=self.node_prompts,
                admitted_connections=self.admitted_connections,
                cli_profiles=self.cli_profiles,
                environ=self.environ,
                byok_egress_broker=self.byok_egress_broker,
                byok_credential_resolver=self.byok_credential_resolver,
                byok_tls_context=self.byok_tls_context,
                approval_resolver=resolver,
            )
        except GraphValidationError as exc:
            raise GraphIntegrityError(f"resume: controller wiring failed — {exc.message}") from exc
        controller.resume()
        return read_arena_projection(
            plan, event_log, request,
            self.arena_authorizer, _NoopArenaReceiptVerifier(),
        )

    def approve(
        self,
        request: ArenaReadRequest,
        *,
        node_id: str,
        decision: str,
    ) -> ArenaProjection:
        """Record a human decision for an approval node and resume the run.

        For ``decision == "approved"``, the full ``approvals.approve`` use case is run
        (validates authority, signature, effects, nonce) and the decision is durably persisted
        BEFORE the run continues — fail-closed at every step.

        For ``decision == "rejected"``, the same authority check is applied (via the injected
        approval authorizer — not merely same-tenant), then the rejection is durably persisted and
        the run is failed closed. The decision string, the target node, and any conflicting prior
        decision are all validated before anything is written.
        """
        plan, identity, _meta = self._load(request)
        self._authorize_mutation(request, identity)
        run_dir = self._run_dir(request)
        event_log = GraphEventLog(run_dir / "controller-events.jsonl", identity)
        self._check_connector_prompts(plan, event_log)

        # Reject any decision string other than the two the domain defines — a direct library caller
        # (unlike the MCP shim) is otherwise silently treated as "rejected" (dual-audit MAJOR).
        if decision not in ("approved", "rejected"):
            raise GraphValidationError(
                "approval_decision", "/decision", "decision must be 'approved' or 'rejected'",
            )
        # Validate the target BEFORE any durable write, so a bogus/non-approval node_id never poisons
        # the ledger and wedges every future resume (dual-audit MAJOR).
        node = self._require_approval_node(plan, node_id)
        self._guard_decision_conflict(run_dir, node_id, decision)

        if decision == "approved":
            self._record_approval(
                request=request, plan=plan, identity=identity,
                event_log=event_log, node_id=node_id, run_dir=run_dir,
            )
        else:
            # "rejected": authorize with the SAME authority as an approval (not merely same-tenant),
            # then DURABLY record the rejection so a crash before the run fails is still recovered.
            self._authorize_decision(request=request, identity=identity, node=node)
            _FileApprovalCommandPort(run_dir).commit_rejection(
                node_id=node_id, attempt=1, approval_id=_approval_id(identity, node_id),
                actor_id=request.subject_id,
                decided_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )

        # Rebuild the resolver from the DURABLE ledger — approve() and resume() now share one source
        # of truth, and the decision just written to approvals.json survives a crash before resume.
        resolver = build_durable_approval_resolver(identity=identity, plan=plan, run_dir=run_dir)
        try:
            controller, _store, event_log = build_execution_controller(
                plan=plan,
                identity=identity,
                out_dir=run_dir,
                node_prompts=self.node_prompts,
                admitted_connections=self.admitted_connections,
                cli_profiles=self.cli_profiles,
                environ=self.environ,
                byok_egress_broker=self.byok_egress_broker,
                byok_credential_resolver=self.byok_credential_resolver,
                byok_tls_context=self.byok_tls_context,
                approval_resolver=resolver,
            )
        except GraphValidationError as exc:
            raise GraphIntegrityError(f"approve: controller wiring failed — {exc.message}") from exc
        controller.resume()
        return read_arena_projection(
            plan, event_log, request,
            self.arena_authorizer, _NoopArenaReceiptVerifier(),
        )

    # ── private helpers ──────────────────────────────────────────────────────

    def _run_dir(self, request: ArenaReadRequest) -> Path:
        # ADDITIVE flat mode (set only by `for_run_dir`, already validated — symlink
        # guard + identity load — at construction time): the run IS this exact
        # directory, no join, nothing to traverse.
        if self._literal_run_dir is not None:
            return self._literal_run_dir
        # Original hosted/multi-tenant mode: validate every segment (no "/", no "..", no
        # absolute) AND assert the resolved path stays inside runs_root — a crafted
        # org/project/run_id must never escape the run root (dual-audit BLOCKER: a
        # forged run-meta OUTSIDE runs_root would otherwise pass the later tenant check).
        org = _safe_segment(request.organization_id, "organization_id")
        project = _safe_segment(request.project_id, "project_id")
        run_id = _safe_segment(request.run_id, "run_id")
        candidate = (self.runs_root / org / project / run_id).resolve()
        root = self.runs_root.resolve()
        if candidate != root and root not in candidate.parents:
            raise GraphIntegrityError("runtime facade: run directory escapes runs_root")
        return candidate

    def _load(
        self, request: ArenaReadRequest,
    ) -> tuple[ExecutionPlan, GraphRunIdentity, dict]:
        """Load plan + identity from the persisted run dir; raise ``GraphIntegrityError`` on any failure."""
        run_dir = self._run_dir(request)
        try:
            return _load_plan_from_run_dir(run_dir)
        except FileNotFoundError as exc:
            raise GraphIntegrityError(
                f"run not found: {request.organization_id}/{request.project_id}/{request.run_id}"
            ) from exc
        except (ValueError, OSError) as exc:
            raise GraphIntegrityError(f"run directory corrupted: {exc}") from exc

    def _authorize_mutation(self, request: ArenaReadRequest, identity: GraphRunIdentity) -> None:
        """Authorize BEFORE any mutation. resume()/approve() re-drive or record on the run, but
        read_arena_projection only authorizes at the END — too late — so mirror its tenant-match +
        authorizer check here, up front, and fail closed so an unauthorized subject can never
        mutate another tenant's run and merely be denied the read afterwards."""
        if (
            request.organization_id != identity.organization_id
            or request.project_id != identity.project_id
            or request.run_id != identity.run_id
        ):
            raise GraphIntegrityError("runtime facade: request tenant does not match the run")
        if not self.arena_authorizer.authorize(request):
            raise GraphIntegrityError("runtime facade: subject is not authorized for this run")

    def _check_connector_prompts(
        self, plan: ExecutionPlan, event_log: GraphEventLog,
    ) -> None:
        """Fail closed if a connector node is non-terminal and has no supplied prompt."""
        receipts = event_log.replay()
        current_states = latest_node_states(plan, receipts)
        for node in plan.nodes:
            if not is_egress_node(plan, node, _ALL_EXECUTOR_TRANSPORTS):
                continue
            node_state = str(current_states[node.node_id]["state"])
            if node_state in ("SUCCEEDED", "FAILED"):
                continue
            if node.node_id not in self.node_prompts:
                raise GraphIntegrityError(
                    f"cannot resume: connector node {node.node_id!r} is {node_state} "
                    "but no prompt was re-supplied; add it to node_prompts and retry"
                )

    def _record_approval(
        self,
        *,
        request: ArenaReadRequest,
        plan: ExecutionPlan,
        identity: GraphRunIdentity,
        event_log: GraphEventLog,
        node_id: str,
        run_dir: Path,
    ) -> None:
        """Run the full ``approvals.approve`` use case, DURABLY persisting the grant to approvals.json.

        The resolver is not recorded here — the caller rebuilds it from the durable ledger via
        ``build_durable_approval_resolver`` so a crash between this commit and the resume still
        re-honors the grant."""
        node = next((n for n in plan.nodes if n.node_id == node_id), None)
        if node is None:
            raise GraphIntegrityError(f"approval node {node_id!r} not found in plan")

        required_role = str(node.approval_policy.get("required_role") or "reviewer")
        snapshot = event_log.verified_snapshot()
        evidence_digest = "sha256:" + snapshot.projection.head_hash

        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
        decided_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = hashlib.sha256(
            f"{identity.run_id}:{node_id}:nonce".encode("utf-8")
        ).hexdigest()
        approval_id = _approval_id(identity, node_id)
        idempotency_key = approval_id

        auth_ctx_raw = json.dumps(
            {
                "organization_id": request.organization_id,
                "project_id": request.project_id,
                "subject_id": request.subject_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        auth_context_digest = "sha256:" + hashlib.sha256(auth_ctx_raw).hexdigest()

        approval_request = ApprovalRequest(
            approval_id=approval_id,
            organization_id=identity.organization_id,
            project_id=identity.project_id,
            graph_digest=identity.graph_digest,
            plan_digest=identity.plan_digest,
            node_id=node_id,
            attempt=1,
            evidence_digest=evidence_digest,
            requested_effects=frozenset(node.required_effects),
            required_role=required_role,
            nonce=nonce,
            expires_at=expires_at,
        )

        req_digest = _request_digest(approval_request)
        approval_decision = ApprovalDecision(
            request_digest=req_digest,
            actor_id=request.subject_id,
            actor_role=required_role,
            decision="approve",
            auth_context_digest=auth_context_digest,
            decided_at=decided_at,
            signature="local-attestation",
        )

        context = AuthenticatedApprovalContext(
            subject_id=request.subject_id,
            organization_id=request.organization_id,
            project_id=request.project_id,
            auth_context_digest=auth_context_digest,
        )

        # Read the CURRENT ledger version so the SECOND approval in a multi-gate DAG is not rejected
        # as stale: the version advances on every commit, so hardcoding 1 failed every gate after the
        # first (dual-audit BLOCKER). The commit re-checks this under the flock, so a concurrent write
        # still fails closed rather than silently overwriting.
        current_version = _load_approvals(run_dir / "approvals.json").get("resource_version", 1)
        target = ApprovalTarget(
            organization_id=identity.organization_id,
            project_id=identity.project_id,
            graph_digest=identity.graph_digest,
            plan_digest=identity.plan_digest,
            node_id=node_id,
            attempt=1,
            evidence_digest=evidence_digest,
            requested_effects=frozenset(node.required_effects),
            resource_version=current_version,
        )

        command_port = _FileApprovalCommandPort(run_dir)
        _approve_use_case(
            approval_request,
            target,
            approval_decision,
            context,
            self.approval_authorizer or _SameTenantApprovalAuthorizer(),
            self.approval_signature_verifier or _LocalApprovalSignatureVerifier(),
            command_port,
            expected_resource_version=current_version,
            idempotency_key=idempotency_key,
            now=now,
        )

    def _require_approval_node(self, plan: ExecutionPlan, node_id: str) -> PlannedNode:
        """Return the APPROVAL node with ``node_id`` or fail closed — a bogus or non-approval node_id
        must never reach a durable write and wedge every future resume (dual-audit MAJOR)."""
        node = next((n for n in plan.nodes if n.node_id == node_id), None)
        if node is None:
            raise GraphIntegrityError(f"approval node {node_id!r} not found in plan")
        if node.kind != NodeKind.APPROVAL.value:
            raise GraphValidationError("approval_node", "/node_id", f"node {node_id!r} is not an approval node")
        return node

    def _guard_decision_conflict(self, run_dir: Path, node_id: str, decision: str) -> None:
        """Refuse a decision that conflicts with one already durably recorded for the node, so the
        ledger can never hold both an approval and a rejection for a node (dual-audit MAJOR)."""
        record = _load_approvals(run_dir / "approvals.json")
        has_approval = any(c.get("node_id") == node_id for c in record.get("commits", []))
        has_rejection = any(r.get("node_id") == node_id for r in record.get("rejections", []))
        if decision == "approved" and has_rejection:
            raise GraphIntegrityError(f"cannot approve node {node_id!r}: a durable rejection already exists")
        if decision == "rejected" and has_approval:
            raise GraphIntegrityError(f"cannot reject node {node_id!r}: a durable approval already exists")

    def _authorize_decision(
        self, *, request: ArenaReadRequest, identity: GraphRunIdentity, node: PlannedNode,
    ) -> None:
        """Run the injected approval authorizer for a human decision, so a REJECTION is gated by the
        SAME authority as an approval — not merely same-tenant (dual-audit: authorization asymmetry).
        The local default authorizes any same-tenant subject; a hosted deployment injects a
        role-checking authorizer, which now governs BOTH approve and reject."""
        approval_request = _rehydrated_request(identity, node)
        auth_ctx_raw = json.dumps(
            {
                "organization_id": request.organization_id,
                "project_id": request.project_id,
                "subject_id": request.subject_id,
            },
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8")
        context = AuthenticatedApprovalContext(
            subject_id=request.subject_id,
            organization_id=request.organization_id,
            project_id=request.project_id,
            auth_context_digest="sha256:" + hashlib.sha256(auth_ctx_raw).hexdigest(),
        )
        authorizer = self.approval_authorizer or _SameTenantApprovalAuthorizer()
        if not authorizer.authorize(approval_request, context):
            raise GraphIntegrityError(f"not authorized to decide approval node {node.node_id!r}")
