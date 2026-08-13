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
  ``plan_persistence.load_plan_from_run_dir`` for the SAME symlink guards and identity
  reconstruction those commands already trust, so opening a flat run this way is no
  weaker than the existing traversal discipline — there is simply nothing left to
  traverse. ``bl graph run --execute --out <dir>`` writes flat (directly into ``<dir>``)
  as of 0.4.0, and ``bl graph approve`` uses this classmethod to open it.
"""

from __future__ import annotations

import logging

import hashlib
import json
import re
import secrets
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from bounded_loops.graph.adapters.connectors.admitted_connection_request import AdmittedConnectionRecord
from bounded_loops.graph.adapters.connectors.local_cli_worker import CliProfile
from bounded_loops.graph.adapters.connectors.provider_catalog import (
    default_catalog_path,
    resolve_cli_profiles,
)
from bounded_loops.graph.adapters.persistence.event_log import GraphEventLog
from bounded_loops.graph.adapters.persistence.local_approval_access import (
    SameTenantArenaAuthorizer,
    _FileApprovalCommandPort,
    _LocalApprovalSignatureVerifier,
    _NoopArenaReceiptVerifier,
    _SameTenantApprovalAuthorizer,
    _safe_segment,
)
from bounded_loops.graph.application.approval_ledger import (
    _approval_id,
    _load_approvals,
    _rehydrated_request,
    build_durable_approval_resolver,
)
from bounded_loops.graph.application.approvals import (
    ApprovalAuthorizationPort,
    ApprovalSignatureVerifierPort,
    ApprovalTarget,
    AuthenticatedApprovalContext,
    approve as _approve_use_case,
    request_digest as _request_digest,
)
from bounded_loops.graph.application.node_spend import RunBudget
from bounded_loops.graph.domain.pricing import PriceTable
from bounded_loops.graph.application.arena_projection import (
    ArenaAuthorizationPort,
    ArenaProjection,
    ArenaReadRequest,
    latest_node_states,
    read_arena_projection,
)
from bounded_loops.graph.application.egress_broker import EgressBroker
from bounded_loops.graph.graph_composition import (
    _ALL_EXECUTOR_TRANSPORTS,
    build_execution_controller,
)
from bounded_loops.graph.application.plan_persistence import load_plan_from_run_dir
from bounded_loops.graph.application.run_graph import is_egress_node
from bounded_loops.graph.domain.approvals import ApprovalDecision, ApprovalRequest
from bounded_loops.graph.domain.authoring import NodeKind
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity
from bounded_loops.graph.domain.plan import ExecutionPlan, PlannedNode

# _approval_id, _load_approvals, and _rehydrated_request are imported from approval_ledger
# (the single canonical implementation) and used internally throughout this module.
# Tests that previously imported them from this module path now import from approval_ledger
# directly (ARCH-07 fix).

# A run-dir path segment: no "/", no "..", no absolute — reject a traversal before it is joined.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


_LOGGER = logging.getLogger(__name__)


def _recorded_catalog(run_dir: Path) -> Path | None:
    """The provider catalog this run was CREATED with, from ``run-meta.json``.

    A continuation has to resolve the same provider map the run did. Before P3 recorded this, a
    catalog that overrode a shipped name — an operator pointing ``claude`` at their own wrapper —
    was silently dropped on resume and approve, and the continuation invoked (and paid for) the
    shipped binary instead. Nothing failed; the wrong CLI just ran.

    Falls back to ``BOUNDED_LOOPS_PROVIDERS`` when the run recorded nothing, which is the case for
    every run created before this field existed.

    A recorded catalog that has since been EDITED is a warning, not a refusal: the operator may have
    legitimately corrected an entry, and refusing would make a resumable run unresumable over a
    file they still have. A recorded catalog that has since been DELETED is left to the wiring
    check, which names the provider it cannot find and how to supply it.
    """
    meta_path = run_dir / "run-meta.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default_catalog_path()
    recorded = meta.get("provider_catalog")
    if not isinstance(recorded, str) or not recorded:
        return default_catalog_path()
    path = Path(recorded)
    expected = meta.get("provider_catalog_sha256")
    if isinstance(expected, str) and expected:
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return path  # let the wiring check name the missing provider
        if actual != expected:
            _LOGGER.warning(
                "provider catalog %s has changed since this run was created; continuing with the "
                "file as it is now. If a provider entry was edited, this run's remaining nodes may "
                "resolve to a different binary than the ones already completed.",
                recorded,
            )
    return path


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
    # Resolved, not shipped: a run created with a provider catalog must be RESUMABLE. Defaulting
    # to the five built-ins meant resume/approve opened the run with a provider map the run's own
    # plan could name providers outside of — and since P3 refuses that at the wiring chokepoint,
    # defaulting narrow here would make every catalog-provider graph unresumable.
    cli_profiles: Mapping[str, CliProfile] = field(
        default_factory=lambda: dict(resolve_cli_profiles(catalog_path=default_catalog_path()))
    )
    environ: Mapping[str, str] | None = None
    node_prompts: Mapping[str, str] = field(default_factory=dict)
    admitted_connections: Mapping[str, AdmittedConnectionRecord] | None = None
    #: The operator's spend ceilings for the runs this facade drives, and the rates that price
    #: them. Present here because a paused run has to be CONTINUABLE: the pause asks the
    #: operator to raise the ceiling, and if no continue path can carry a new one then the
    #: pause is a dead end rather than a decision point. ``resume`` also takes a per-call
    #: override, which is the ordinary way a ceiling gets raised or lowered.
    run_budget: RunBudget | None = None
    price_table: PriceTable | None = None
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
        provider_catalog: Path | None = None,
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
        3. ``plan_persistence.load_plan_from_run_dir`` must be able to reconstruct an
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
            load_plan_from_run_dir(resolved)
        except FileNotFoundError as exc:
            raise GraphIntegrityError(f"'{run_dir}' is not a run directory: {exc}") from exc
        except (ValueError, OSError, GraphValidationError) as exc:
            raise GraphIntegrityError(f"'{run_dir}' is not a valid run directory: {exc}") from exc
        return cls(
            runs_root=resolved,  # inert in this mode — every lookup returns _literal_run_dir
            arena_authorizer=arena_authorizer or SameTenantArenaAuthorizer(),
            # Precedence, most explicit first: a map the caller built > a catalog the caller
            # named > the catalog THIS RUN was created with > BOUNDED_LOOPS_PROVIDERS > shipped.
            # The recorded one has to outrank the env var: it is what the completed nodes actually
            # ran, and resolving a continuation differently is how the wrong binary gets invoked.
            cli_profiles=(
                cli_profiles if cli_profiles is not None
                else dict(resolve_cli_profiles(
                    catalog_path=provider_catalog or _recorded_catalog(run_dir),
                ))
            ),
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

    def resume(
        self, request: ArenaReadRequest, *, run_budget: RunBudget | None = None,
        price_table: PriceTable | None = None,
    ) -> ArenaProjection:
        """Resume an interrupted run, returning the post-resume projection.

        ``run_budget`` raises or lowers the spend ceiling for this continuation, overriding the
        facade's own. That is the entire answer to "the run paused, now what": one call with a
        new number. Without it a budget-paused run could not be continued from any shipped
        entry point — the controller refuses to continue one with no ceiling declared, and
        every caller here passed none.

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
                run_budget=run_budget if run_budget is not None else self.run_budget,
                price_table=price_table if price_table is not None else self.price_table,
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
        run_budget: RunBudget | None = None,
        price_table: PriceTable | None = None,
    ) -> ArenaProjection:
        """Record a human decision for an approval node and resume the run.

        Takes the same spend override as ``resume``: approving a checkpoint continues the run,
        and continuing spends money, so this path needs a ceiling exactly as much.

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

        # Prove the controller CAN be assembled before writing the decision. P3 added a
        # provider check inside ``build_execution_controller``, and with the write first, an
        # operator approving without the run's provider catalog committed the approval and then hit
        # a refusal — leaving the run AWAITING_APPROVAL with the decision already recorded, and
        # every later approve failing identically. That is the P2-B closed door exactly: a new
        # refusal that wedges a previously-continuable run. A dry assembly costs one object
        # construction and keeps the ledger untouched when the wiring cannot work.
        self._assert_assemblable(plan=plan, identity=identity, run_dir=run_dir)

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
                run_budget=run_budget if run_budget is not None else self.run_budget,
                price_table=price_table if price_table is not None else self.price_table,
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
            return load_plan_from_run_dir(run_dir)
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
        # An UNPREDICTABLE nonce (256 bits from a CSPRNG), not a hash of public values.
        # It was previously derived from `run_id:node_id`, both of which anyone with read
        # access to the run directory can see, so the value a signature is computed over
        # was fully predictable for every run and node. That is not what a nonce is for.
        #
        # Randomising it is safe here because nothing depends on it being reproducible:
        # idempotency is keyed on `idempotency_key` (the deterministic `approval_id`), the
        # digest is built and compared inside this single call, and the durable-rehydration
        # path documents that it never re-validates this field. `token_hex(32)` yields the
        # same 64-hex-character shape the previous SHA-256 derivation did.
        nonce = secrets.token_hex(32)
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

    def _assert_assemblable(
        self, *, plan: ExecutionPlan, identity: GraphRunIdentity, run_dir: Path,
    ) -> None:
        """Raise if this deployment could not build a controller for *plan* — before any write.

        Uses the same ``build_execution_controller`` the real continuation uses, so the check cannot
        drift from what it is checking. Assembly reads and validates; it starts no node and appends
        no receipt, so calling it twice costs one wasted object and no durable effect.
        """
        try:
            build_execution_controller(
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
                run_budget=self.run_budget,
                price_table=self.price_table,
            )
        except GraphValidationError as exc:
            raise GraphIntegrityError(
                f"approve: refusing to record a decision this deployment could not then act on — "
                f"{exc.message}"
            ) from exc

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
