"""Local, same-tenant approval and arena access for a single-operator deployment.

These five adapters were declared inside ``graph_runtime_facade.py`` — concrete adapters in a
composition module, the same misfiling P3 corrected across the graph engine. Filed here so the
ports they satisfy have one findable implementation, and so a deployment replacing one of them
(a real signature verifier, a real multi-tenant authorizer) has an obvious seam to replace it at.

Two of them do LESS than their names might suggest, and say so out loud rather than reading as
completed work:

* ``_NoopArenaReceiptVerifier`` performs no SIGNATURE verification. The event log still verifies
  its hash chain on every replay, so tampering is still caught — what is absent is
  non-repudiation, which needs a key this local path does not hold.
* ``SameTenantArenaAuthorizer`` / ``_SameTenantApprovalAuthorizer`` compare the caller's tenant to
  the run's. That is a real check, and it is the ONLY check: they carry no notion of a role, so a
  deployment that needs "who may approve what" must supply its own.

``_FileApprovalCommandPort`` is the durable half — an approval decision has to survive the process
that recorded it, so it is written under an exclusive lock and fsynced before it is acknowledged.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from bounded_loops.graph.application.approval_ledger import _load_approvals
from bounded_loops.graph.application.approvals import (
    ApprovalCommand,
    ApprovalCommit,
    ApprovalDecision,
    ApprovalRequest,
    AuthenticatedApprovalContext,
)
from bounded_loops.graph.application.arena_projection import ArenaReadRequest
from bounded_loops.graph.domain.errors import GraphIntegrityError, GraphValidationError
from bounded_loops.graph.domain.events import GraphRunIdentity, StoredGraphEvent

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
    #: The repair round this decision belongs to, stamped into the durable record so a later round
    #: cannot inherit it. Defaults to 0 so every existing construction site is unchanged and a
    #: round-0 decision keeps exactly the coordinates it always had (Grok 2).
    _repair_round: int = 0

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
                    # WHO decided, alongside the subject the authorizer checked. `actor_id` is
                    # the tenant on a local run and cannot be anything else, so on its own this
                    # receipt could not answer "who approved this irreversible effect".
                    # `decided_by_source` travels with the name because the name is NOT
                    # authenticated locally, and a name presented without that caveat claims
                    # more than we know.
                    "decided_by": command.decision.decided_by,
                    "decided_by_source": command.decision.decided_by_source,
                    "decided_at": command.decision.decided_at,
                    # Record the random nonce and the digest the decision was made over.
                    # Without these the request can never be reconstructed, so a signature
                    # over it would be unverifiable after the fact — which would make the
                    # nonce pointless however random it is. A hosted deployment that injects
                    # a real signature verifier re-checks the recorded signature against
                    # this digest. (Full independent reconstruction of the digest would also
                    # need `evidence_digest` and `expires_at`; recording the digest itself
                    # avoids depending on that.)
                    "nonce": command.request.nonce,
                    "request_digest": command.decision.request_digest,
                    # The round the human decided IN. A repair resets the approval node and re-runs
                    # the suffix, so a grant made in round 0 was made about different evidence; the
                    # resolver keys on this so round 1 must ask again.
                    "repair_round": self._repair_round,
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
                    "repair_round": self._repair_round,
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
    """Durably replace *path* with *data* — the approvals ledger is an authority file.

    Three defects were fixed here together, because they are one write:

    * **No fsync (CON-02).** `write_bytes` + `os.replace` makes the rename atomic but
      leaves the CONTENT in the page cache, so a power loss could resurrect the file
      with the rename applied and the bytes missing. Every other durable writer in this
      repository (`hash_chain_events`, `event_log`, `run_store`, `config_writer`) fsyncs;
      this one did not, which falsified "the decision is durably persisted BEFORE the run
      continues". The directory is fsynced too, so the rename itself survives a crash.
    * **Predictable temp path (M-4).** `path.with_suffix(".tmp")` is a fixed name, so a
      local process could pre-create or symlink it. `O_EXCL | O_NOFOLLOW` on a
      randomly-named temp refuses both, and the temp is created 0600 from the start
      rather than inheriting the umask.
    * The temp is always cleaned up, so a failed write cannot litter the run directory.

    The temp lives in the target's own directory because `os.replace` cannot cross a
    filesystem boundary.
    """
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, 0o600)
    except OSError as exc:
        raise GraphIntegrityError(f"cannot create a temp file beside {path}: {exc}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)  # force the mode regardless of umask
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        # Fsync the DIRECTORY so the rename entry itself is durable, not just the bytes.
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise GraphIntegrityError(f"cannot durably write {path}: {exc}") from exc
    finally:
        # No-op on success (os.replace consumed it); real cleanup on any failure.
        try:
            os.unlink(tmp)
        except OSError:
            pass


# ── facade ────────────────────────────────────────────────────────────────────
